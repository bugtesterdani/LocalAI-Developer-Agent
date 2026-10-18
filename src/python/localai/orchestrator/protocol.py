"""Antwortformat der Agenten: Begründung + genau ein JSON-Objekt (Spezifikation §5)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_READ_FILES = 8
# Felder, an denen ein unverpacktes Abschlussergebnis erkennbar ist.
RESULT_KEYS = {"message_de", "tasks", "patch", "test_plan", "test_patch", "verdict", "findings", "fix_patch", "hypothesis"}
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


class ProtocolError(Exception):
    """Antwort des Modells entspricht nicht dem Format; wird dem Modell zurückgemeldet."""


@dataclass
class FileRef:
    path: str
    start: int | None = None
    end: int | None = None


@dataclass
class Action:
    kind: str  # list | read | search | finish
    reasoning: str
    path: str = "."
    depth: int = 3
    files: list[FileRef] = field(default_factory=list)
    query: str = ""
    reason: str = ""
    result: dict[str, Any] = field(default_factory=dict)


def _last_json_object(text: str) -> tuple[str, str]:
    """Liefert (JSON-Text, Begründung davor). Bevorzugt den letzten ```json-Block."""
    fences = list(_FENCE.finditer(text))
    if fences:
        last = fences[-1]
        return last.group(1).strip(), text[:last.start()]
    # Ohne Codeblock: letztes ausgeglichenes {...} (Klammern in Zeichenketten werden übersprungen).
    end = text.rfind("}")
    depth = 0
    in_string = False
    i = end
    while i >= 0:
        ch = text[i]
        if in_string:
            if ch == '"':
                backslashes = 0
                j = i - 1
                while j >= 0 and text[j] == "\\":
                    backslashes += 1
                    j -= 1
                if backslashes % 2 == 0:
                    in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                return text[i:end + 1], text[:i]
        i -= 1
    raise ProtocolError("No JSON object found. End your answer with exactly one ```json block.")


def _clean_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("Every path must be a non-empty string.")
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path or "."


def parse(text: str) -> Action:
    raw, reasoning = _last_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ProtocolError(f"The JSON block is invalid ({err.msg} at position {err.pos}). Send it again.") from err
    if not isinstance(data, dict):
        raise ProtocolError("The JSON block must be an object.")
    kind = data.get("action")
    reasoning = reasoning.strip()
    if kind is None and RESULT_KEYS & set(data):
        # Kleinere Modelle lassen die Hülle {"action": "finish", "result": …} gern weg und liefern das Ergebnis direkt.
        # Eindeutig, weil Aktionen nie diese Felder haben; die Prüfung je Rolle folgt in validate_result.
        return Action("finish", reasoning, result=data)
    if kind == "list":
        depth = data.get("depth", 3)
        if not isinstance(depth, int) or not 1 <= depth <= 5:
            raise ProtocolError("depth must be an integer from 1 to 5.")
        return Action("list", reasoning, path=_clean_path(data.get("path", ".")), depth=depth)
    if kind == "read":
        files = data.get("files")
        if isinstance(files, list) and files and all(isinstance(f, str) for f in files):
            files = [{"path": f} for f in files]
        if not isinstance(files, list) or not files:
            raise ProtocolError('read needs "files": [{"path": "...", "start": 1, "end": 200}].')
        if len(files) > MAX_READ_FILES:
            raise ProtocolError(f"Read at most {MAX_READ_FILES} files per request.")
        refs = []
        for f in files:
            if not isinstance(f, dict):
                raise ProtocolError("Each entry in files must be an object with path.")
            start, end = f.get("start"), f.get("end")
            for v in (start, end):
                if v is not None and (not isinstance(v, int) or v < 1):
                    raise ProtocolError("start and end must be positive line numbers.")
            if start is not None and end is not None and end < start:
                raise ProtocolError("end must not be before start.")
            refs.append(FileRef(_clean_path(f.get("path")), start, end))
        return Action("read", reasoning, files=refs, reason=str(data.get("reason", "")))
    if kind == "search":
        query = data.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ProtocolError("search needs a non-empty query of at most 200 characters.")
        return Action("search", reasoning, path=_clean_path(data.get("path", ".")), query=query.strip(),
                      reason=str(data.get("reason", "")))
    if kind == "finish":
        result = data.get("result")
        if not isinstance(result, dict):
            raise ProtocolError('finish needs "result": { ... } as described for your role.')
        return Action("finish", reasoning, result=result)
    raise ProtocolError('Unknown action. Use "list", "read", "search" or "finish". A final result must be wrapped: '
                        '{"action": "finish", "result": { ... }}.')


# ------------------------------------------------------------------ Ergebnisprüfung je Rolle

def _str(result: dict[str, Any], key: str, required: bool = True) -> str:
    value = result.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise ProtocolError(f'result.{key} must be a non-empty string.')
    return value


def _str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProtocolError(f"{key} must be a list of strings.")
    return [v.strip().replace("\\", "/") for v in value if v.strip()]


_HUNK = re.compile(r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", re.M)


def check_diff(text: str, field: str) -> None:
    """Grobe Formprüfung eines Unified Diffs (Patch.Apply in M4 prüft streng)."""
    if not re.search(r"^--- (a/|/dev/null)", text, re.M) or not re.search(r"^\+\+\+ b/", text, re.M) or not _HUNK.search(text):
        raise ProtocolError(f"result.{field} must be a unified diff with file headers: '--- a/<path>' (or '--- /dev/null' "
                            "for a new file), '+++ b/<path>', then hunks starting with '@@ -start,count +start,count @@'.")


def validate_result(role: str, result: dict[str, Any]) -> dict[str, Any]:
    """Prüft das Abschlussergebnis; Fehlermeldungen gehen ans Modell zurück (englisch)."""
    _str(result, "message_de")
    if role == "Planner":
        tasks = result.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ProtocolError("result.tasks must be a non-empty list.")
        for t in tasks:
            if not isinstance(t, dict):
                raise ProtocolError("Each task must be an object.")
            for key in ("id", "title", "description"):
                _str(t, key)
            if t.get("role") not in ("Coder", "Tester"):
                raise ProtocolError('task.role must be "Coder" or "Tester".')
            t["reads"] = _str_list(t.get("reads"), "task.reads")
            t["writes"] = _str_list(t.get("writes"), "task.writes")
            t["depends_on"] = _str_list(t.get("depends_on"), "task.depends_on")
        result["acceptance"] = _str_list(result.get("acceptance"), "result.acceptance")
    elif role == "Coder":
        check_diff(_str(result, "patch"), "patch")
        result["files"] = _str_list(result.get("files"), "result.files")
    elif role == "Tester":
        plan = result.get("test_plan")
        if not isinstance(plan, list) or not plan:
            raise ProtocolError("result.test_plan must be a non-empty list.")
        if _str(result, "test_patch", required=False).strip():
            check_diff(result["test_patch"], "test_patch")
        # Tests wurden nicht ausgeführt – unabhängig davon, was das Modell behauptet.
        result["executed"] = False
    elif role == "Debugger":
        _str(result, "hypothesis")
        # Leer erlaubt: der Befund ist mit einer Codeänderung nicht zu beheben (Reparaturschleife endet dann).
        if _str(result, "fix_patch", required=False).strip():
            check_diff(result["fix_patch"], "fix_patch")
    elif role == "Reviewer":
        if result.get("verdict") not in ("changes_needed", "ready_for_user_review"):
            raise ProtocolError('result.verdict must be "changes_needed" or "ready_for_user_review".')
        findings = result.get("findings", [])
        if not isinstance(findings, list):
            raise ProtocolError("result.findings must be a list.")
        for f in findings:
            if not isinstance(f, dict) or f.get("severity") not in ("high", "medium", "low"):
                raise ProtocolError('Each finding needs severity "high", "medium" or "low".')
            _str(f, "finding")
        result["findings"] = findings
        if result["verdict"] == "ready_for_user_review" and any(f["severity"] in ("high", "medium") for f in findings):
            result["verdict"] = "changes_needed"
    return result
