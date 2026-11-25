"""Model Gateway: Chat mit Streaming, Abbruch und Watchdog (Spezifikation §9, R14) – für Ollama und Edge0.

Nur Standardbibliothek. Verbindungen nur zu Loopback-Adressen bzw. – ausdrücklich freigegeben – zu einer privaten
LAN-IP (der Orchestrator hat sonst keinen Netzwerkzugang). Bei Ollama wird der Kontext pro Request gesetzt
(``num_ctx``), nie global; bei Edge0 legt ihn der Serverstart fest (``--context``).

Modellserver (``backend`` in config/models.json): ``OllamaGateway`` (/api/chat, NDJSON) und ``Edge0Gateway``
(edge0-infer serve, OpenAI-kompatibel /v1/chat/completions, SSE). ``RoutingGateway`` verteilt auf beide, wenn
Profile unterschiedliche Server nutzen.

Abbruch: ``CancelToken.cancel()`` schließt die HTTP-Verbindung aus einem anderen Thread; der Server beendet
daraufhin die Generierung. Watchdog: bleiben Tokens länger als ``stall_warning`` aus, wird ``on_stall``
gemeldet (Anzeige „hängt?“); nach ``stall_abort`` wird der Request abgebrochen.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import pathlib
import queue
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

LOOPBACK_HOSTS = {"localhost"}


class GatewayError(Exception):
    """Modellserver nicht erreichbar oder Fehler während der Generierung."""


class Cancelled(Exception):
    """Request wurde abgebrochen (Nutzer, Not-Aus, Watchdog)."""

    def __init__(self, reason: str = "abgebrochen") -> None:
        super().__init__(reason)
        self.reason = reason


class StallAbort(GatewayError):
    """Watchdog hat den Request nach zu langer Stille beendet."""


class RepetitionLoop(GatewayError):
    """Das Modell wiederholt dasselbe Textstück immer wieder (Generierung abgebrochen)."""


def find_repetition(text: str, min_repeats: int = 12, max_period: int = 80, min_span: int = 200) -> str | None:
    """Endet ``text`` mit einem Stück, das sich ``min_repeats``-mal direkt wiederholt? Liefert das Stück oder None.

    Absichtlich streng (viele Wiederholungen über mindestens ``min_span`` Zeichen), damit Code mit gleichförmigen
    Zeilen (Tabellen, ``++``-Diffzeilen) nicht fälschlich abgebrochen wird."""
    for period in range(2, max_period + 1):
        span = period * min_repeats
        if len(text) < max(span, min_span):
            continue
        unit = text[-period:]
        if not unit.strip():
            continue
        repeats = max(min_repeats, -(-min_span // period))
        if len(text) >= period * repeats and text[-period * repeats:] == unit * repeats:
            return unit
    return None


class OutOfMemory(GatewayError):
    """Der Modellserver meldet Speichermangel (für die Kalibrierung relevant)."""


OOM_MARKERS = ("out of memory", "cuda error", "insufficient memory", "failed to allocate", "unable to allocate",
               "requires more system memory", "llama runner process has terminated", "model requires more")


def classify_error(message: str) -> GatewayError:
    lower = message.lower()
    if any(marker in lower for marker in OOM_MARKERS):
        return OutOfMemory(message)
    return GatewayError(message)


class CancelToken:
    """Abbruchsignal; schließt eine registrierte Verbindung sofort."""

    def __init__(self, parent: CancelToken | None = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closers: list[Callable[[], None]] = []
        self.reason = ""
        self._parent = parent
        if parent is not None:
            parent._register(self._cancel_from_parent)

    def _cancel_from_parent(self) -> None:
        self.cancel(self._parent.reason if self._parent else "abgebrochen")

    def _register(self, closer: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._closers.append(closer)
                return
        closer()

    def _unregister(self, closer: Callable[[], None]) -> None:
        with self._lock:
            if closer in self._closers:
                self._closers.remove(closer)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "abgebrochen") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            closers, self._closers = self._closers, []
        for closer in closers:
            try:
                closer()
            except Exception:  # noqa: BLE001 – Abbruch darf nie scheitern
                pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason or "abgebrochen")

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass
class ChatResult:
    content: str
    thinking: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    prompt_seconds: float = 0.0
    output_seconds: float = 0.0
    load_seconds: float = 0.0
    total_seconds: float = 0.0
    first_token_seconds: float | None = None
    done_reason: str = ""
    # Funktionsaufrufe, die Ollama aus der Antwort gelesen hat (gpt-oss ruft trotz fehlender Tools gern „Funktionen“
    # auf, statt die Aktion als Text zu schreiben): [{"name": …, "arguments": {…}}]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tokens_per_second(self) -> float:
        return self.output_tokens / self.output_seconds if self.output_seconds > 0 else 0.0


@dataclass
class ChatRequest:
    model: str
    messages: list[dict[str, str]]
    num_ctx: int
    num_predict: int
    temperature: float = 0.2
    think: bool | None = None
    keep_alive: str = "10m"
    extra_options: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None  # nur Edge0 (reasoning_effort der Chat-API), z. B. "none" oder "low"

    def body(self) -> dict[str, Any]:
        options = {"num_ctx": self.num_ctx, "num_predict": self.num_predict, "temperature": self.temperature}
        options.update(self.extra_options)
        body: dict[str, Any] = {
            "model": self.model, "messages": self.messages, "stream": True, "options": options,
            "keep_alive": self.keep_alive,
        }
        if self.think is not None:
            body["think"] = self.think
        return body


@dataclass
class StreamCallbacks:
    """Rückrufe während der Generierung (werden im Thread des Requests aufgerufen)."""

    on_content: Callable[[str], None] = lambda _t: None
    on_thinking: Callable[[str], None] = lambda _t: None
    on_stall: Callable[[float], None] = lambda _s: None
    on_resume: Callable[[], None] = lambda: None
    on_progress: Callable[[int, float], None] = lambda _tokens, _elapsed: None


def is_loopback_url(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_loopback(url: str, allow_lan: bool = False, service: str = "Ollama",
                      section: str = "ollama") -> urllib.parse.SplitResult:
    """Standard: nur Loopback. Mit ``allow_lan`` (ollama.allowLan bzw. edge0.allowLan) zusätzlich eine private
    LAN-Adresse als IP-Literal (10/8, 172.16/12, 192.168/16, fd00::/8, Link-Local) – nie ein öffentliches Ziel, kein
    DNS-Name."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise GatewayError(f"{service}-Adresse muss http://<ip>:<port> sein, nicht {url!r}")
    if is_loopback_url(url):
        return parts
    host = parts.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError as err:
        raise GatewayError(f"{service} nur über Loopback oder eine private LAN-IP (als Zahl) erlaubt, nicht {host}") from err
    if not allow_lan:
        raise GatewayError(f"{service} nur über Loopback erlaubt, nicht {host} – für einen anderen Rechner im LAN "
                           f"\"allowLan\": true im Abschnitt \"{section}\" von config/models.local.json setzen")
    if not (address.is_private or address.is_link_local) or address.is_unspecified or address.is_multicast:
        raise GatewayError(f"{host} ist keine private LAN-Adresse – nicht erlaubt")
    return parts


class _ThinkSplitter:
    """Trennt <think>…</think> aus dem Inhalt, falls ein Modell Thinking inline liefert."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, text: str) -> list[tuple[bool, str]]:
        self.pending += text
        out: list[tuple[bool, str]] = []
        while self.pending:
            marker = self.CLOSE if self.inside else self.OPEN
            idx = self.pending.find(marker)
            if idx >= 0:
                if idx:
                    out.append((self.inside, self.pending[:idx]))
                self.pending = self.pending[idx + len(marker):]
                self.inside = not self.inside
                continue
            # Ende könnte ein angeschnittener Marker sein: zurückhalten.
            keep = 0
            for n in range(min(len(marker) - 1, len(self.pending)), 0, -1):
                if marker.startswith(self.pending[-n:]):
                    keep = n
                    break
            emit = self.pending[:len(self.pending) - keep]
            if emit:
                out.append((self.inside, emit))
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return out

    def flush(self) -> list[tuple[bool, str]]:
        rest, self.pending = self.pending, ""
        return [(self.inside, rest)] if rest else []


class ServerBusy(GatewayError):
    """Der Modellserver bearbeitet gerade eine andere Anfrage (Edge0: HTTP 429) – später erneut versuchen."""


class ModelGateway(Protocol):
    """Was Orchestrator, Agenten und Kalibrierung von einem Modellserver brauchen."""

    url: str
    label: str

    def version(self) -> str: ...

    def list_models(self) -> list[dict[str, Any]]: ...

    def running(self) -> list[dict[str, Any]]: ...

    def unload(self, model: str) -> None: ...

    def chat(self, request: ChatRequest, cancel: CancelToken | None = None,
             callbacks: StreamCallbacks | None = None) -> ChatResult: ...


def version_text(gateway: Any, version: str) -> str:
    """„Ollama 0.12.3“ bzw. „Edge0 edge0-infer“; mehrere Server bringen ihre Namen schon in ``version`` mit."""
    label = getattr(gateway, "label", "")
    return f"{label} {version}" if label else version


class _StreamingGateway:
    """Gemeinsamer Teil aller Modellserver: HTTP (Loopback oder freigegebenes LAN), Streaming in eigenem Thread, Abbruch,
    Watchdog und Erkennung von Wiederholungsschleifen. Unterklassen liefern Anfragekörper, Auswertung einer
    Antwortzeile und das Ergebnis (Ollama: NDJSON von /api/chat, Edge0: OpenAI-SSE von /v1/chat/completions)."""

    label = "Modellserver"
    config_section = ""
    default_port = 80
    chat_path = "/"

    def __init__(self, url: str, stall_warning: float = 45.0, stall_abort: float = 900.0, connect_timeout: float = 5.0,
                 poll: float = 0.25, allow_lan: bool = False, busy_wait: float = 0.0, busy_retry: float = 1.0) -> None:
        parts = validate_loopback(url, allow_lan, self.label, self.config_section)
        self.url = url.rstrip("/")
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or self.default_port
        self.stall_warning = stall_warning
        self.stall_abort = stall_abort
        self.connect_timeout = connect_timeout
        self.poll = poll
        self.busy_wait = busy_wait
        self.busy_retry = busy_retry

    # ------------------------------------------------------------------ Bausteine für Unterklassen
    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _error(self, data: bytes, status: int) -> GatewayError:
        return classify_error(_error_text(data, status))

    def _chat_body(self, request: ChatRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _parse_line(self, line: bytes, state: dict[str, Any]) -> tuple[str, str, bool] | None:
        """Eine Zeile des Datenstroms → (Thinking-Stück, Inhalt-Stück, fertig) oder None (Zeile ohne Bedeutung)."""
        raise NotImplementedError

    def _result(self, request: ChatRequest, state: dict[str, Any], content: str, thinking: str, tokens: int,
                first_token: float | None, started: float) -> ChatResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ einfache Abfragen
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 timeout: float = 10.0) -> tuple[int, bytes]:
        headers = self._headers()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as err:
            raise GatewayError(f"{self.label} unter {self.url} nicht erreichbar: {err}") from err
        finally:
            conn.close()

    def _get_json(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        status, data = self._request(method, path, body, timeout)
        if status >= 400:
            raise self._error(data, status)
        try:
            return json.loads(data or b"{}")
        except json.JSONDecodeError as err:
            raise GatewayError(f"Ungültige Antwort von {self.label}: {data[:200]!r}") from err

    # ------------------------------------------------------------------ Streaming-Chat
    def chat(self, request: ChatRequest, cancel: CancelToken | None = None,
             callbacks: StreamCallbacks | None = None) -> ChatResult:
        cancel = cancel or CancelToken()
        callbacks = callbacks or StreamCallbacks()
        busy_since = time.monotonic()
        while True:
            try:
                return self._chat_once(request, cancel, callbacks)
            except ServerBusy as err:
                # Nur eine Generierung gleichzeitig (Edge0): kommt vor der ersten Ausgabe, also gefahrlos wiederholbar.
                waited = time.monotonic() - busy_since
                if waited >= self.busy_wait:
                    raise GatewayError(f"{self.label} ist belegt ({err}) – nach {waited:.0f} s Warten aufgegeben") from err
                if cancel.wait(self.busy_retry):
                    cancel.raise_if_cancelled()

    def _chat_once(self, request: ChatRequest, cancel: CancelToken, callbacks: StreamCallbacks) -> ChatResult:
        cancel.raise_if_cancelled()
        headers = self._headers()
        payload = json.dumps(self._chat_body(request)).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)

        sockets: list[socket.socket] = []

        def close() -> None:
            # Bei "Connection: close" gibt http.client den Socket an die Antwort ab (conn.sock = None):
            # deshalb den beim Verbinden gemerkten Socket schließen.
            for sock in [*sockets, conn.sock]:
                if sock is None:
                    continue
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()

        cancel._register(close)
        started = time.monotonic()
        content: list[str] = []
        thinking: list[str] = []
        state: dict[str, Any] = {}
        splitter = _ThinkSplitter()
        first_token: float | None = None
        tokens = 0
        last_chunk = started
        stalled = False
        last_progress = started
        finished = False
        try:
            try:
                conn.connect()
                if conn.sock is not None:
                    sockets.append(conn.sock)
                    # Zeitlimit nur für den Verbindungsaufbau: Bis zur ersten Antwort lädt der Server ggf. das Modell
                    # (bei großen Modellen oder über das LAN deutlich länger) – das überwacht der Watchdog.
                    conn.sock.settimeout(None)
                cancel.raise_if_cancelled()
                conn.request("POST", self.chat_path, body=payload, headers=headers)
            except (OSError, http.client.HTTPException) as err:
                cancel.raise_if_cancelled()
                raise GatewayError(f"{self.label} unter {self.url} nicht erreichbar: {err}") from err
            # Antwortkopf und Datenstrom blockierend in eigenem Thread; hier wird nur mit Zeitlimit gewartet (Watchdog).
            # (Ein Socket-Timeout würde das Antwortobjekt unbrauchbar machen.)
            chunks: queue.Queue[bytes | BaseException] = queue.Queue()

            def pump() -> None:
                try:
                    response = conn.getresponse()
                    if response.status >= 400:
                        chunks.put(self._error(response.read(), response.status))
                        return
                    while True:
                        data = response.read1(65536)
                        chunks.put(data)
                        if not data:
                            return
                except BaseException as err:  # noqa: BLE001 – an den wartenden Thread weiterreichen
                    chunks.put(err)

            threading.Thread(target=pump, name=f"{self.label.lower()}-stream", daemon=True).start()
            buffer = b""
            while True:
                cancel.raise_if_cancelled()
                try:
                    item = chunks.get(timeout=self.poll)
                except queue.Empty:
                    item = None
                if isinstance(item, GatewayError):
                    raise item
                if isinstance(item, BaseException):
                    cancel.raise_if_cancelled()
                    raise GatewayError(f"Verbindung zu {self.label} unterbrochen: {item}") from item
                chunk = item
                now = time.monotonic()
                if chunk is None:
                    silent = now - last_chunk
                    if silent >= self.stall_abort:
                        close()
                        raise StallAbort(f"Watchdog: seit {silent:.0f} s keine Tokens – Request abgebrochen")
                    if not stalled and silent >= self.stall_warning:
                        stalled = True
                        callbacks.on_stall(silent)
                    continue
                if chunk == b"":
                    break
                last_chunk = now
                if stalled:
                    stalled = False
                    callbacks.on_resume()
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    parsed = self._parse_line(line, state)
                    if parsed is None:
                        continue
                    think_piece, piece, done = parsed
                    if think_piece:
                        thinking.append(think_piece)
                        callbacks.on_thinking(think_piece)
                    if piece:
                        for inside, text in splitter.feed(piece):
                            (thinking if inside else content).append(text)
                            (callbacks.on_thinking if inside else callbacks.on_content)(text)
                    if (think_piece or piece) and tokens % 32 == 0:
                        # Kleine Modelle geraten gelegentlich in eine Endlosschleife ("…error.error.error…"): Tokens kommen,
                        # der Watchdog greift also nicht. Früh abbrechen statt bis zur Ausgabegrenze zu warten.
                        tail = "".join(content[-256:]) if piece else "".join(thinking[-256:])
                        unit = find_repetition(tail[-2000:])
                        if unit is not None:
                            close()
                            raise RepetitionLoop(f"Wiederholungsschleife abgebrochen (\"{unit[:40]}\" wiederholt sich)")
                    if think_piece or piece:
                        tokens += 1
                        if first_token is None:
                            first_token = now - started
                    if now - last_progress >= 1.0:
                        last_progress = now
                        callbacks.on_progress(tokens, now - started)
                    if done:
                        finished = True
                        break
                if finished:
                    break
            for inside, text in splitter.flush():
                (thinking if inside else content).append(text)
                (callbacks.on_thinking if inside else callbacks.on_content)(text)
            if not finished:
                cancel.raise_if_cancelled()
                raise GatewayError(f"{self.label} hat die Antwort vorzeitig beendet")
        finally:
            cancel._unregister(close)
            conn.close()
        return self._result(request, state, "".join(content), "".join(thinking), tokens, first_token, started)


class OllamaGateway(_StreamingGateway):
    """Ollama über /api/chat (NDJSON). Kontext je Anfrage (``num_ctx``), Modelle per Tag."""

    label = "Ollama"
    config_section = "ollama"
    default_port = 11434
    chat_path = "/api/chat"

    def __init__(self, url: str = "http://127.0.0.1:11434", stall_warning: float = 45.0, stall_abort: float = 900.0,
                 connect_timeout: float = 5.0, poll: float = 0.25, allow_lan: bool = False) -> None:
        super().__init__(url, stall_warning, stall_abort, connect_timeout, poll, allow_lan)

    def version(self) -> str:
        return str(self._get_json("GET", "/api/version").get("version", "?"))

    def list_models(self) -> list[dict[str, Any]]:
        return list(self._get_json("GET", "/api/tags").get("models", []))

    def show(self, model: str) -> dict[str, Any]:
        return dict(self._get_json("POST", "/api/show", {"model": model}))

    def running(self) -> list[dict[str, Any]]:
        """Entspricht ``ollama ps``: geladene Modelle mit size und size_vram."""
        return list(self._get_json("GET", "/api/ps").get("models", []))

    def unload(self, model: str) -> None:
        try:
            self._get_json("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
        except GatewayError:
            pass

    def _chat_body(self, request: ChatRequest) -> dict[str, Any]:
        return request.body()

    def _parse_line(self, line: bytes, state: dict[str, Any]) -> tuple[str, str, bool] | None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as err:
            raise GatewayError(f"Ungültige Antwort von Ollama: {line[:200]!r}") from err
        if "error" in message:
            raise classify_error(str(message["error"]))
        msg = message.get("message") or {}
        for call in msg.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                state.setdefault("tool_calls", []).append({"name": str(function.get("name") or ""),
                                                           "arguments": function.get("arguments")})
        done = bool(message.get("done"))
        if done:
            state["final"] = message
        return msg.get("thinking") or "", msg.get("content") or "", done

    def _result(self, request: ChatRequest, state: dict[str, Any], content: str, thinking: str, tokens: int,
                first_token: float | None, started: float) -> ChatResult:
        final = state.get("final") or {}
        ns = 1e-9
        return ChatResult(
            content=content, thinking=thinking, model=str(final.get("model", request.model)),
            prompt_tokens=int(final.get("prompt_eval_count") or 0), output_tokens=int(final.get("eval_count") or tokens),
            prompt_seconds=float(final.get("prompt_eval_duration") or 0) * ns,
            output_seconds=float(final.get("eval_duration") or 0) * ns,
            load_seconds=float(final.get("load_duration") or 0) * ns,
            total_seconds=time.monotonic() - started, first_token_seconds=first_token,
            done_reason=str(final.get("done_reason", "")), tool_calls=list(state.get("tool_calls", [])),
        )


# Profiloptionen (Ollama-Namen) → Felder der Edge0-Chat-API. Alles andere (num_ctx, repeat_last_n, …) kennt Edge0 nicht;
# unbekannte Felder beantwortet der Server mit HTTP 400, darum werden sie nicht gesendet.
EDGE0_OPTIONS = {"repeat_penalty": "repetition_penalty", "repetition_penalty": "repetition_penalty",
                 "top_k": "top_k", "top_p": "top_p", "seed": "seed"}
_EDGE0_INT_OPTIONS = {"top_k", "seed"}
EDGE0_KEY_ENV = "LOCALAI_EDGE0_API_KEY"


class Edge0Gateway(_StreamingGateway):
    """Edge0 (``edge0-infer serve``) über die OpenAI-kompatible Chat-API mit SSE-Streaming.

    Eigenheiten des Servers: genau ein Modell (Alias) je Server; Kontextlänge beim Serverstart fest (``--context``),
    nicht je Anfrage; nur eine Generierung gleichzeitig (sonst HTTP 429 → hier warten und erneut versuchen); unbekannte
    Felder → HTTP 400 (darum nur bekannte Felder senden); außerhalb von Loopback verlangt der Server ein Token
    (``--api-key-file``), hier aus ``apiKeyFile`` bzw. der Umgebungsvariable LOCALAI_EDGE0_API_KEY."""

    label = "Edge0"
    config_section = "edge0"
    default_port = 8000
    chat_path = "/v1/chat/completions"

    def __init__(self, url: str = "http://127.0.0.1:8000", stall_warning: float = 45.0, stall_abort: float = 900.0,
                 connect_timeout: float = 5.0, poll: float = 0.25, allow_lan: bool = False, api_key_file: str = "",
                 busy_wait: float = 600.0, busy_retry: float = 1.0) -> None:
        super().__init__(url, stall_warning, stall_abort, connect_timeout, poll, allow_lan, busy_wait, busy_retry)
        self.api_key_file = api_key_file
        self._api_key: str | None = None
        self._loopback = is_loopback_url(self.url)
        # think=false → reasoning_effort "none", bis ein Server das ablehnt (dann ohne das Feld weiter).
        self._implicit_effort = True

    # ------------------------------------------------------------------ Zugang
    def _key(self) -> str:
        if self._api_key is None:
            key = os.environ.get(EDGE0_KEY_ENV, "").strip()
            if not key and self.api_key_file:
                try:
                    key = pathlib.Path(self.api_key_file).read_text(encoding="utf-8-sig").strip()
                except OSError as err:
                    raise GatewayError(f"Edge0-Schlüsseldatei {self.api_key_file} nicht lesbar: "
                                       f"{err.strerror or err}") from err
                if not key:
                    raise GatewayError(f"Edge0-Schlüsseldatei {self.api_key_file} ist leer")
            if not key and not self._loopback:
                raise GatewayError("Edge0 auf einem anderen Rechner verlangt ein Token: dort edge0-infer serve mit "
                                   "--api-key-file starten und hier \"apiKeyFile\" im Abschnitt \"edge0\" (oder die "
                                   f"Umgebungsvariable {EDGE0_KEY_ENV}) setzen")
            self._api_key = key
        return self._api_key

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _error(self, data: bytes, status: int) -> GatewayError:
        text = _error_text(data, status)
        if status == 429:
            return ServerBusy(text)
        if status in (401, 403):
            return GatewayError(f"Edge0 lehnt das Token ab (HTTP {status}): {text}")
        return classify_error(text)

    # ------------------------------------------------------------------ einfache Abfragen
    def version(self) -> str:
        status, data = self._request("GET", "/health")
        if status >= 400:
            raise GatewayError(f"Edge0 unter {self.url} nicht bereit: {_error_text(data, status)}")
        try:
            info = self._get_json("GET", "/status", timeout=5.0)
        except GatewayError:
            info = {}
        version = info.get("version") if isinstance(info, dict) else None
        return str(version) if version else "edge0-infer"

    def list_models(self) -> list[dict[str, Any]]:
        """Edge0 bedient genau ein Modell – sein Alias (``--alias``, Standard z. B. ``edge0_35b``) ist der Modellname."""
        data = self._get_json("GET", "/v1/models")
        items = data.get("data", []) if isinstance(data, dict) else []
        return [{"name": str(m["id"]), "model": str(m["id"]), "digest": ""}
                for m in items if isinstance(m, dict) and m.get("id")]

    def show(self, model: str) -> dict[str, Any]:
        return dict(self._get_json("GET", "/v1/models/" + urllib.parse.quote(model, safe="")))

    def running(self) -> list[dict[str, Any]]:
        return []  # keine Aufteilung GPU/CPU wie bei „ollama ps“

    def unload(self, model: str) -> None:
        return None  # das Modell bleibt geladen, solange der Server läuft

    # ------------------------------------------------------------------ Chat
    def chat(self, request: ChatRequest, cancel: CancelToken | None = None,
             callbacks: StreamCallbacks | None = None) -> ChatResult:
        implicit = not request.reasoning_effort and request.think is False and self._implicit_effort
        try:
            return super().chat(request, cancel, callbacks)
        except (Cancelled, StallAbort, RepetitionLoop, OutOfMemory):
            raise
        except GatewayError as err:
            if implicit and "reasoning" in str(err).lower():
                self._implicit_effort = False
                return super().chat(request, cancel, callbacks)
            raise

    def _effort(self, request: ChatRequest) -> str | None:
        if request.reasoning_effort:
            return request.reasoning_effort
        if request.think is False and self._implicit_effort:
            return "none"
        return None

    def _chat_body(self, request: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model, "messages": [_openai_message(m) for m in request.messages], "stream": True,
            "stream_options": {"include_usage": True}, "max_tokens": request.num_predict,
            "temperature": request.temperature,
        }
        for key, value in request.extra_options.items():
            target = EDGE0_OPTIONS.get(key)
            if target:
                body[target] = int(value) if target in _EDGE0_INT_OPTIONS else float(value)
        effort = self._effort(request)
        if effort:
            body["reasoning_effort"] = effort
        return body

    def _parse_line(self, line: bytes, state: dict[str, Any]) -> tuple[str, str, bool] | None:
        text = line.decode("utf-8", errors="replace").strip()
        if text.startswith((":", "event:", "id:", "retry:")):
            return None
        if not text.startswith("data:"):
            raise GatewayError(f"Ungültige Antwort von Edge0: {text[:200]!r}")
        data = text[5:].strip()
        if data == "[DONE]":
            return "", "", True
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as err:
            raise GatewayError(f"Ungültige Antwort von Edge0: {data[:200]!r}") from err
        if not isinstance(obj, dict):
            raise GatewayError(f"Ungültige Antwort von Edge0: {data[:200]!r}")
        if obj.get("error"):
            # Fehler nach Beginn des Datenstroms: Edge0 sendet ein Fehlerobjekt und schließt ohne [DONE].
            raise classify_error(_error_text(json.dumps(obj).encode(), 500))
        if obj.get("model"):
            state["model"] = str(obj["model"])
        if isinstance(obj.get("usage"), dict):
            state["usage"] = obj["usage"]
        thinking = content = ""
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            thinking += str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            content += str(delta.get("content") or "")
            for call in delta.get("tool_calls") or []:
                if isinstance(call, dict):
                    _merge_tool_call(state.setdefault("calls", {}), call)
            if choice.get("finish_reason"):
                state["finish"] = str(choice["finish_reason"])
        return thinking, content, False

    def _result(self, request: ChatRequest, state: dict[str, Any], content: str, thinking: str, tokens: int,
                first_token: float | None, started: float) -> ChatResult:
        finish = str(state.get("finish", ""))
        if finish == "error":
            raise GatewayError("Edge0 hat die Generierung mit einem Fehler beendet")
        usage = state.get("usage") or {}
        total = time.monotonic() - started
        # Edge0 liefert keine eigenen Zeiten: Vorverarbeitung ≈ Zeit bis zur ersten Ausgabe, Ausgabe = Rest.
        calls = [{"name": c["name"], "arguments": _tool_arguments(c["arguments"])}
                 for _, c in sorted(state.get("calls", {}).items())]
        return ChatResult(
            content=content, thinking=thinking, model=str(state.get("model", request.model)),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0) or tokens,
            prompt_seconds=first_token or 0.0,
            output_seconds=max(total - first_token, 0.0) if first_token is not None else 0.0,
            load_seconds=0.0, total_seconds=total, first_token_seconds=first_token, done_reason=finish,
            tool_calls=calls,
        )


def _openai_message(message: dict[str, Any]) -> dict[str, Any]:
    """Nur Felder, die die Chat-API kennt (Edge0 lehnt unbekannte Felder ab)."""
    out: dict[str, Any] = {"role": message.get("role", "user"), "content": message.get("content") or ""}
    for key in ("tool_calls", "tool_call_id", "name"):
        if message.get(key):
            out[key] = message[key]
    return out


def _merge_tool_call(calls: dict[int, dict[str, Any]], call: dict[str, Any]) -> None:
    """Funktionsaufrufe kommen im Datenstrom stückweise (Name einmal, Argumente als Textstücke) – je ``index`` sammeln."""
    index = call.get("index")
    index = index if isinstance(index, int) else len(calls)
    entry = calls.setdefault(index, {"name": "", "arguments": ""})
    function = call.get("function") or {}
    if function.get("name") and not entry["name"]:
        entry["name"] = str(function["name"])
    arguments = function.get("arguments")
    if isinstance(arguments, str) and isinstance(entry["arguments"], str):
        entry["arguments"] += arguments
    elif isinstance(arguments, dict):
        entry["arguments"] = arguments


def _tool_arguments(raw: Any) -> Any:
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


class RoutingGateway:
    """Mehrere Modellserver gleichzeitig (Profile mit unterschiedlichem ``backend``): jede Anfrage geht an den Server,
    dem ihr Modell zugeordnet ist. Ist nur einer der Server erreichbar, bleiben dessen Modelle nutzbar."""

    label = ""

    def __init__(self, gateways: dict[str, Any], routes: dict[str, str], primary: str) -> None:
        self.gateways = gateways
        self.routes = routes
        self.primary = primary
        self.url = gateways[primary].url
        self.unreachable: dict[str, str] = {}

    def _for(self, model: str) -> Any:
        return self.gateways[self.routes.get(model, self.primary)]

    def version(self) -> str:
        parts: list[str] = []
        errors: list[GatewayError] = []
        for gateway in self.gateways.values():
            try:
                parts.append(f"{gateway.label} {gateway.version()}")
            except GatewayError as err:
                errors.append(err)
                parts.append(f"{gateway.label} nicht erreichbar")
        if len(errors) == len(self.gateways):
            raise errors[0]
        return " · ".join(parts)

    def list_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        errors: list[GatewayError] = []
        self.unreachable = {}
        for name, gateway in self.gateways.items():
            try:
                listed = gateway.list_models()
            except GatewayError as err:
                errors.append(err)
                self.unreachable[name] = str(err)
                continue
            models.extend(m for m in listed if self.routes.get(str(m.get("name")), name) == name)
        if len(errors) == len(self.gateways):
            raise errors[0]
        return models

    def show(self, model: str) -> dict[str, Any]:
        return self._for(model).show(model)

    def running(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for gateway in self.gateways.values():
            try:
                result.extend(gateway.running())
            except GatewayError:
                pass
        return result

    def unload(self, model: str) -> None:
        self._for(model).unload(model)

    def chat(self, request: ChatRequest, cancel: CancelToken | None = None,
             callbacks: StreamCallbacks | None = None) -> ChatResult:
        return self._for(request.model).chat(request, cancel, callbacks)


def _error_text(data: bytes, status: int) -> str:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP {status}: {data[:300].decode(errors='replace')}"
    if not isinstance(payload, dict):
        return f"HTTP {status}: {str(payload)[:300]}"
    error = payload.get("error", data[:300].decode(errors="replace"))
    if isinstance(error, dict):  # OpenAI-/Anthropic-Format (Edge0): {"error": {"message": …, "param": …}}
        text = str(error.get("message") or error.get("type") or error)
        param = error.get("param")
        return f"{text} (Feld {param})" if param and str(param) not in text else text
    return str(error)
