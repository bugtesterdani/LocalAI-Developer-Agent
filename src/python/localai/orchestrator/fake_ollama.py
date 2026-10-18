"""Simulierter Ollama-Server für Tests und für eine Vorführung der Oberfläche ohne echtes Modell.

Spricht die benutzten Endpunkte (/api/version, /api/tags, /api/show, /api/ps, /api/chat, /api/generate) und
streamt Antworten tokenweise. Das eingebaute „Skript“ erkennt die Rolle am Systemprompt und liefert
plausible Aktionen, sodass ein vollständiger Run mit allen fünf Rollen durchläuft.

Aufruf (nur Loopback): ``python -m localai.orchestrator.fake_ollama --port 11435 --delay 0.02``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODELS = ("qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen3:8b", "qwen3-coder:30b")


def _fence(obj: dict[str, Any], reasoning: str) -> str:
    return f"{reasoning}\n```json\n{json.dumps(obj, ensure_ascii=False, indent=1)}\n```"


def _files_from_listing(prompt: str) -> list[str]:
    return re.findall(r"^([^\s<>][^\n]*?)  \(\d+ Bytes\)$", prompt, re.M)


def scripted_answer(body: dict[str, Any]) -> tuple[str, str]:
    """Liefert (Inhalt, Thinking) passend zur Rolle im Systemprompt."""
    system = body["messages"][0]["content"]
    user = body["messages"][-1]["content"]
    role = (re.search(r"You are the (\w+) agent", system) or [None, "Planner"])[1]
    thinking = f"{role}: considering the task step by step." if body.get("think") else ""
    files = _files_from_listing(user) or ["README.md"]
    code = [f for f in files if f.endswith((".py", ".cs", ".cpp", ".ps1", ".bat", ".hdev"))] or files
    first = code[0]
    has_read = "source=\"File.Read" in user or "source=\"File.ReadMany" in user
    if role == "Planner":
        if not has_read:
            return _fence({"action": "read", "files": [{"path": first, "start": 1, "end": 80}],
                           "reason": "understand the main module"}, "I need to see the main file first."), thinking
        tests = "tests/test_" + first.rsplit("/", 1)[-1].split(".")[0] + ".py"
        return _fence({"action": "finish", "result": {
            "message_de": "Ich teile die Arbeit in Umsetzung und unabhängige Tests auf.",
            "acceptance": ["Funktion verarbeitet leere Eingaben", "Bestehende Tests bleiben grün"],
            "tasks": [
                {"id": "T1", "role": "Coder", "title": "Funktion ergänzen", "description": f"Extend {first}.",
                 "reads": [first], "writes": [first], "depends_on": []},
                {"id": "T2", "role": "Tester", "title": "Grenzfälle testen", "description": "Write edge case tests.",
                 "reads": [first], "writes": [tests], "depends_on": []}]}}, "The plan has two independent tasks."), thinking
    if role == "Coder":
        if not has_read:
            return _fence({"action": "read", "files": [{"path": first}], "reason": "file to change"}, "Reading first."), thinking
        return _fence({"action": "finish", "result": {
            "message_de": f"Leere Eingaben werden in {first} abgefangen.", "files": [first],
            "patch": f"--- a/{first}\n+++ b/{first}\n@@ -1,1 +1,3 @@\n+# handle empty input\n+",
            "rationale": "Guard clause for empty input."}}, "The change is small."), thinking
    if role == "Tester":
        return _fence({"action": "finish", "result": {
            "message_de": "Zwei Grenzfälle sind abgedeckt.", "executed": True,
            "test_plan": [{"name": "test_empty", "purpose": "empty input", "expected": "no exception"},
                          {"name": "test_large", "purpose": "large input", "expected": "same result"}],
            "test_patch": "--- /dev/null\n+++ b/tests/test_edge.py\n@@ -0,0 +1,2 @@\n+def test_empty():\n+    pass\n"}},
            "Tests derived from the requirements."), thinking
    if role == "Reviewer":
        if "Repair attempt" in user:
            return _fence({"action": "finish", "result": {"message_de": "Der Befund ist behoben.",
                                                          "verdict": "ready_for_user_review", "findings": []}},
                          "The fix resolves the finding."), thinking
        return _fence({"action": "finish", "result": {
            "message_de": "Ein Grenzfall fehlt noch.", "verdict": "changes_needed",
            "findings": [{"severity": "medium", "file": first, "line": 1, "finding": "None input is not handled",
                          "evidence": "guard only checks empty string"}]}}, "One finding."), thinking
    if role == "Debugger":
        return _fence({"action": "finish", "result": {
            "message_de": "None wird jetzt wie eine leere Eingabe behandelt.", "hypothesis": "Missing None check.",
            "fix_patch": f"--- a/{first}\n+++ b/{first}\n@@ -1,1 +1,2 @@\n+# handle None\n",
            "regression_test": "test_none_input"}}, "Minimal fix."), thinking
    return _fence({"action": "finish", "result": {"message_de": "?"}}, ""), thinking


@dataclass
class FakeOllama:
    """Konfigurierbarer Fake. ``responder(body) -> (content, thinking)`` ersetzt das Skript."""

    models: tuple[str, ...] = DEFAULT_MODELS
    delay: float = 0.0
    chunk_chars: int = 6
    first_token_delay: float = 0.0
    responder: Callable[[dict[str, Any]], tuple[str, str]] = scripted_answer
    error: str | None = None
    chars_per_token: float = 3.5
    vram_fraction: float = 1.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    disconnects: int = 0
    completed: int = 0
    loaded: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self, port: int = 0) -> FakeOllama:
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

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/version":
                    self._json({"version": "0.0.0-fake"})
                elif self.path == "/api/tags":
                    self._json({"models": [{"name": m, "model": m, "digest": "sha256:" + hashlib.sha256(m.encode()).hexdigest(),
                                            "details": {"quantization_level": "Q4_K_M"}} for m in fake.models]})
                elif self.path == "/api/ps":
                    with fake.lock:
                        loaded = dict(fake.loaded)
                    self._json({"models": [{"name": m, "size": 9 * 1024**3, "size_vram": int(9 * 1024**3 * fake.vram_fraction),
                                            "context_length": ctx} for m, ctx in loaded.items()]})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/show":
                    self._json({"details": {"quantization_level": "Q4_K_M"}, "license": "Apache-2.0"})
                    return
                if self.path == "/api/generate":
                    with fake.lock:
                        fake.loaded.pop(body.get("model"), None)
                    self._json({"done": True})
                    return
                if self.path != "/api/chat":
                    self._json({"error": "not found"}, 404)
                    return
                with fake.lock:
                    fake.requests.append(body)
                if body.get("model") not in fake.models:
                    self._json({"error": f"model '{body.get('model')}' not found"}, 404)
                    return
                if fake.error:
                    self._json({"error": fake.error}, 500)
                    return
                with fake.lock:
                    fake.loaded = {body["model"]: body.get("options", {}).get("num_ctx", 2048)}
                content, thinking = fake.responder(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                prompt_chars = sum(len(m.get("content", "")) for m in body.get("messages", []))
                started = time.monotonic()
                try:
                    if fake.first_token_delay:
                        time.sleep(fake.first_token_delay)
                    pieces = 0
                    for kind, text in (("thinking", thinking), ("content", content)):
                        for i in range(0, len(text), fake.chunk_chars):
                            msg = {"role": "assistant", "content": "", kind: text[i:i + fake.chunk_chars]}
                            if kind == "thinking":
                                msg["content"] = ""
                            self.wfile.write(json.dumps({"model": body["model"], "message": msg, "done": False}).encode() + b"\n")
                            self.wfile.flush()
                            pieces += 1
                            if fake.delay:
                                time.sleep(fake.delay)
                    elapsed = max(time.monotonic() - started, 1e-3)
                    final = {"model": body["model"], "message": {"role": "assistant", "content": ""}, "done": True,
                             "done_reason": "stop", "prompt_eval_count": int(prompt_chars / fake.chars_per_token),
                             "prompt_eval_duration": int(0.1e9), "eval_count": pieces, "eval_duration": int(elapsed * 1e9),
                             "load_duration": 0, "total_duration": int(elapsed * 1e9)}
                    self.wfile.write(json.dumps(final).encode() + b"\n")
                    self.wfile.flush()
                    with fake.lock:
                        fake.completed += 1
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    with fake.lock:
                        fake.disconnects += 1

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="fake-ollama", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="localai.orchestrator.fake_ollama")
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--delay", type=float, default=0.02, help="Sekunden pro Token-Stück")
    args = parser.parse_args()
    fake = FakeOllama(delay=args.delay).start(args.port)
    print(f"Simulierter Ollama-Server läuft auf {fake.url} (Strg+C beendet)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
