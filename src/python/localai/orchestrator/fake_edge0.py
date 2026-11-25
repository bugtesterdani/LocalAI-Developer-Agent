"""Simulierter Edge0-Server (``edge0-infer serve``) für Tests und eine Vorführung ohne echtes Modell.

Spricht die benutzten Endpunkte wie Edge0: ``/health``, ``/status``, ``/v1/models``, ``/v1/models/{id}`` und
``/v1/chat/completions`` (Streaming per SSE). Verhält sich in den Punkten wie der echte Server, auf die sich das Gateway
verlassen muss: genau ein Modell (Alias), nur eine Generierung gleichzeitig (sonst HTTP 429), unbekannte Felder →
HTTP 400, mit gesetztem Token nur mit ``Authorization: Bearer``. Die Antworten liefert dasselbe Skript wie beim
simulierten Ollama-Server, sodass ein vollständiger Run mit allen fünf Rollen durchläuft.

Aufruf (nur Loopback): ``python -m localai.orchestrator.fake_edge0 --port 8001 --delay 0.02``
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from localai.orchestrator.fake_ollama import SUMMARIZER_MARK, scripted_answer, summary_answer

CHAT_FIELDS = {"model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "temperature",
               "top_p", "top_k", "repetition_penalty", "seed", "stop", "tools", "tool_choice", "reasoning_effort"}
MESSAGE_FIELDS = {"role", "content", "tool_calls", "tool_call_id", "name"}


@dataclass
class FakeEdge0:
    """Konfigurierbarer Fake. ``responder(body) -> (content, thinking[, calls])`` ersetzt das Skript."""

    alias: str = "edge0_35b"
    delay: float = 0.0
    chunk_chars: int = 6
    first_token_delay: float = 0.0
    responder: Callable[[dict[str, Any]], tuple[Any, ...]] = scripted_answer
    summarizer: Callable[[dict[str, Any]], tuple[str, str]] | None = summary_answer
    error: str | None = None  # HTTP 500 vor dem Datenstrom
    stream_error: str | None = None  # Fehlerobjekt mitten im Datenstrom (danach Verbindung zu, ohne [DONE])
    drop_after_chunks: int | None = None  # Verbindung nach so vielen Stücken ohne Abschluss schließen
    api_key: str | None = None  # gesetzt: jede Anfrage braucht "Authorization: Bearer <api_key>"
    reject_reasoning_effort: bool = False
    busy: int = 0  # so viele Chat-Anfragen zusätzlich mit 429 ablehnen
    finish_reasons: list[str] = field(default_factory=list)  # je Anfrage der Reihe nach, danach "stop"
    chars_per_token: float = 3.5
    requests: list[dict[str, Any]] = field(default_factory=list)
    authorizations: list[str | None] = field(default_factory=list)
    rejected: list[int] = field(default_factory=list)  # HTTP-Status abgelehnter Chat-Anfragen
    disconnects: int = 0
    completed: int = 0

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()
        self.generating = False

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self, port: int = 0) -> FakeEdge0:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: object) -> None:
                pass

            def _json(self, obj: Any, status: int = 200) -> None:
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _fail(self, status: int, message: str, kind: str = "invalid_request_error", param: str | None = None,
                      code: str | None = None) -> None:
                self._json({"error": {"message": message, "type": kind, "param": param, "code": code}}, status)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization")
                with fake.lock:
                    fake.authorizations.append(header)
                if fake.api_key is not None and header != f"Bearer {fake.api_key}":
                    self._fail(401, "invalid api key", "authentication_error")
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path == "/health":
                    self._json({"status": "ok"})
                elif self.path == "/status":
                    with fake.lock:
                        self._json({"version": "0.1.0-fake", "requests": len(fake.requests)})
                elif self.path == "/v1/models":
                    self._json({"object": "list", "data": [{"id": fake.alias, "object": "model", "owned_by": "edge0"}]})
                elif self.path == f"/v1/models/{fake.alias}":
                    self._json({"id": fake.alias, "object": "model", "owned_by": "edge0"})
                else:
                    self._fail(404, "not found", code="not_found")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self._authorized():
                    return
                if self.path != "/v1/chat/completions":
                    self._fail(404, "not found", code="not_found")
                    return
                with fake.lock:
                    fake.requests.append(body)
                status = self._validate(body)
                if status:
                    with fake.lock:
                        fake.rejected.append(status)
                    return
                with fake.lock:
                    if fake.busy > 0 or fake.generating:
                        fake.busy = max(fake.busy - 1, 0)
                        fake.rejected.append(429)
                        busy = True
                    else:
                        fake.generating = True
                        busy = False
                if busy:
                    self._fail(429, "another request is running", "rate_limit_error", code="busy")
                    return
                try:
                    self._stream(body)
                finally:
                    with fake.lock:
                        fake.generating = False

            def _validate(self, body: dict[str, Any]) -> int:
                unknown = sorted(set(body) - CHAT_FIELDS)
                if unknown:
                    self._fail(400, f"unknown field `{unknown[0]}`", param=unknown[0], code="unknown_field")
                    return 400
                for message in body.get("messages") or []:
                    extra = sorted(set(message) - MESSAGE_FIELDS)
                    if extra:
                        self._fail(400, f"unknown field `messages[].{extra[0]}`", param=f"messages.{extra[0]}")
                        return 400
                if "reasoning_effort" in body and fake.reject_reasoning_effort:
                    self._fail(400, "unsupported value", param="reasoning_effort")
                    return 400
                if body.get("model") != fake.alias:
                    self._fail(404, f"model '{body.get('model')}' not found", code="model_not_found")
                    return 404
                if fake.error:
                    self._fail(500, fake.error, "server_error")
                    return 500
                return 0

            def _event(self, obj: Any) -> None:
                data = obj if isinstance(obj, str) else json.dumps(obj)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()

            def _stream(self, body: dict[str, Any]) -> None:
                system = (body.get("messages") or [{}])[0].get("content", "")
                if fake.summarizer is not None and system.startswith(SUMMARIZER_MARK):
                    answer = fake.summarizer(body)
                else:
                    answer = fake.responder(body)
                content, thinking = answer[0], answer[1]
                calls = list(answer[2]) if len(answer) > 2 else []
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                prompt_chars = sum(len(m.get("content") or "") for m in body.get("messages", []))

                def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
                    return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": fake.alias,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

                try:
                    if fake.first_token_delay:
                        time.sleep(fake.first_token_delay)
                    pieces = 0
                    for kind, text in (("reasoning_content", thinking), ("content", content)):
                        for i in range(0, len(text), fake.chunk_chars):
                            self._event(chunk({kind: text[i:i + fake.chunk_chars]}))
                            pieces += 1
                            if fake.stream_error and pieces >= 2:
                                self._event({"error": {"message": fake.stream_error, "type": "server_error"}})
                                return
                            if fake.drop_after_chunks is not None and pieces >= fake.drop_after_chunks:
                                return
                            if fake.delay:
                                time.sleep(fake.delay)
                    for index, call in enumerate(calls):
                        arguments = json.dumps(call.get("arguments") or {})
                        half = len(arguments) // 2
                        self._event(chunk({"tool_calls": [{"index": index, "id": f"call_{index}", "type": "function",
                                                           "function": {"name": call["name"], "arguments": arguments[:half]}}]}))
                        self._event(chunk({"tool_calls": [{"index": index, "function": {"arguments": arguments[half:]}}]}))
                        pieces += 1
                    with fake.lock:
                        finish = fake.finish_reasons.pop(0) if fake.finish_reasons else ("tool_calls" if calls else "stop")
                    self._event(chunk({}, finish))
                    if (body.get("stream_options") or {}).get("include_usage"):
                        prompt = int(prompt_chars / fake.chars_per_token)
                        self._event({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": fake.alias,
                                     "choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": pieces,
                                                              "total_tokens": prompt + pieces}})
                    self._event("[DONE]")
                    with fake.lock:
                        fake.completed += 1
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    with fake.lock:
                        fake.disconnects += 1

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="fake-edge0", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="localai.orchestrator.fake_edge0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--delay", type=float, default=0.02, help="Sekunden pro Token-Stück")
    parser.add_argument("--alias", default="edge0_35b")
    args = parser.parse_args()
    fake = FakeEdge0(alias=args.alias, delay=args.delay).start(args.port)
    print(f"Simulierter Edge0-Server läuft auf {fake.url} mit Modell {fake.alias} (Strg+C beendet)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
