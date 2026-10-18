"""Model Gateway: Ollama-Chat mit Streaming, Abbruch und Watchdog (Spezifikation §9, R14).

Nur Standardbibliothek. Verbindungen ausschließlich zu Loopback-Adressen (Ollama läuft lokal, der
Orchestrator hat sonst keinen Netzwerkzugang). Der Kontext wird pro Request gesetzt (``num_ctx``),
nie global.

Abbruch: ``CancelToken.cancel()`` schließt die HTTP-Verbindung aus einem anderen Thread; Ollama beendet
daraufhin die Generierung. Watchdog: bleiben Tokens länger als ``stall_warning`` aus, wird ``on_stall``
gemeldet (Anzeige „hängt?“); nach ``stall_abort`` wird der Request abgebrochen.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import queue
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LOOPBACK_HOSTS = {"localhost"}


class GatewayError(Exception):
    """Ollama nicht erreichbar oder Fehler während der Generierung."""


class Cancelled(Exception):
    """Request wurde abgebrochen (Nutzer, Not-Aus, Watchdog)."""

    def __init__(self, reason: str = "abgebrochen") -> None:
        super().__init__(reason)
        self.reason = reason


class StallAbort(GatewayError):
    """Watchdog hat den Request nach zu langer Stille beendet."""


class OutOfMemory(GatewayError):
    """Ollama meldet Speichermangel (für die Kalibrierung relevant)."""


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


def validate_loopback(url: str, allow_lan: bool = False) -> urllib.parse.SplitResult:
    """Standard: nur Loopback. Mit ``allow_lan`` (ollama.allowLan) zusätzlich eine private LAN-Adresse als
    IP-Literal (10/8, 172.16/12, 192.168/16, fd00::/8, Link-Local) – nie ein öffentliches Ziel, kein DNS-Name."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise GatewayError(f"Ollama-Adresse muss http://<ip>:<port> sein, nicht {url!r}")
    if is_loopback_url(url):
        return parts
    host = parts.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError as err:
        raise GatewayError(f"Ollama nur über Loopback oder eine private LAN-IP (als Zahl) erlaubt, nicht {host}") from err
    if not allow_lan:
        raise GatewayError(f"Ollama nur über Loopback erlaubt, nicht {host} – für einen anderen Rechner im LAN "
                           "\"allowLan\": true in config/models.local.json setzen")
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


class OllamaGateway:
    def __init__(self, url: str = "http://127.0.0.1:11434", stall_warning: float = 45.0, stall_abort: float = 900.0,
                 connect_timeout: float = 5.0, poll: float = 0.25, allow_lan: bool = False) -> None:
        parts = validate_loopback(url, allow_lan)
        self.url = url.rstrip("/")
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 11434
        self.stall_warning = stall_warning
        self.stall_abort = stall_abort
        self.connect_timeout = connect_timeout
        self.poll = poll

    # ------------------------------------------------------------------ einfache Abfragen
    def _get_json(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            data = response.read()
            if response.status >= 400:
                raise classify_error(_error_text(data, response.status))
            return json.loads(data or b"{}")
        except (OSError, http.client.HTTPException) as err:
            raise GatewayError(f"Ollama unter {self.url} nicht erreichbar: {err}") from err
        finally:
            conn.close()

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

    # ------------------------------------------------------------------ Streaming-Chat
    def chat(self, request: ChatRequest, cancel: CancelToken | None = None,
             callbacks: StreamCallbacks | None = None) -> ChatResult:
        cancel = cancel or CancelToken()
        callbacks = callbacks or StreamCallbacks()
        cancel.raise_if_cancelled()
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
        splitter = _ThinkSplitter()
        first_token: float | None = None
        tokens = 0
        last_chunk = started
        stalled = False
        last_progress = started
        final: dict[str, Any] = {}
        try:
            try:
                conn.connect()
                if conn.sock is not None:
                    sockets.append(conn.sock)
                    # Zeitlimit nur für den Verbindungsaufbau: Bis zur ersten Antwort lädt Ollama ggf. das Modell
                    # (bei großen Modellen oder über das LAN deutlich länger) – das überwacht der Watchdog.
                    conn.sock.settimeout(None)
                cancel.raise_if_cancelled()
                conn.request("POST", "/api/chat", body=json.dumps(request.body()).encode(),
                             headers={"Content-Type": "application/json"})
            except (OSError, http.client.HTTPException) as err:
                cancel.raise_if_cancelled()
                raise GatewayError(f"Ollama unter {self.url} nicht erreichbar: {err}") from err
            # Antwortkopf und Datenstrom blockierend in eigenem Thread; hier wird nur mit Zeitlimit gewartet (Watchdog).
            # (Ein Socket-Timeout würde das Antwortobjekt unbrauchbar machen.)
            chunks: queue.Queue[bytes | BaseException] = queue.Queue()

            def pump() -> None:
                try:
                    response = conn.getresponse()
                    if response.status >= 400:
                        chunks.put(classify_error(_error_text(response.read(), response.status)))
                        return
                    while True:
                        data = response.read1(65536)
                        chunks.put(data)
                        if not data:
                            return
                except BaseException as err:  # noqa: BLE001 – an den wartenden Thread weiterreichen
                    chunks.put(err)

            threading.Thread(target=pump, name="ollama-stream", daemon=True).start()
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
                    raise GatewayError(f"Verbindung zu Ollama unterbrochen: {item}") from item
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
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError as err:
                        raise GatewayError(f"Ungültige Antwort von Ollama: {line[:200]!r}") from err
                    if "error" in message:
                        raise classify_error(str(message["error"]))
                    msg = message.get("message") or {}
                    think_piece = msg.get("thinking") or ""
                    if think_piece:
                        thinking.append(think_piece)
                        callbacks.on_thinking(think_piece)
                    piece = msg.get("content") or ""
                    if piece:
                        for inside, text in splitter.feed(piece):
                            (thinking if inside else content).append(text)
                            (callbacks.on_thinking if inside else callbacks.on_content)(text)
                    if think_piece or piece:
                        tokens += 1
                        if first_token is None:
                            first_token = now - started
                    if now - last_progress >= 1.0:
                        last_progress = now
                        callbacks.on_progress(tokens, now - started)
                    if message.get("done"):
                        final = message
                        break
                if final:
                    break
            for inside, text in splitter.flush():
                (thinking if inside else content).append(text)
                (callbacks.on_thinking if inside else callbacks.on_content)(text)
            if not final:
                cancel.raise_if_cancelled()
                raise GatewayError("Ollama hat die Antwort vorzeitig beendet")
        finally:
            cancel._unregister(close)
            conn.close()
        ns = 1e-9
        return ChatResult(
            content="".join(content), thinking="".join(thinking), model=str(final.get("model", request.model)),
            prompt_tokens=int(final.get("prompt_eval_count") or 0), output_tokens=int(final.get("eval_count") or tokens),
            prompt_seconds=float(final.get("prompt_eval_duration") or 0) * ns,
            output_seconds=float(final.get("eval_duration") or 0) * ns,
            load_seconds=float(final.get("load_duration") or 0) * ns,
            total_seconds=time.monotonic() - started, first_token_seconds=first_token,
            done_reason=str(final.get("done_reason", "")),
        )


def _error_text(data: bytes, status: int) -> str:
    try:
        return str(json.loads(data).get("error", data[:300].decode(errors="replace")))
    except (json.JSONDecodeError, AttributeError):
        return f"HTTP {status}: {data[:300].decode(errors='replace')}"
