"""Antwortformat der Agenten: Begründung + genau ein JSON-Objekt (Spezifikation §5)."""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from localai.worker import patching

MAX_READ_FILES = 8
# Felder, an denen ein unverpacktes Abschlussergebnis erkennbar ist.
RESULT_KEYS = {"message_de", "tasks", "patch", "test_plan", "test_patch", "verdict", "findings", "fix_patch", "hypothesis"}
# Rolle → Feld mit dem Unified Diff des Ergebnisses.
PATCH_FIELD = {"Coder": "patch", "Tester": "test_patch", "Debugger": "fix_patch"}
_FENCE_LINE = re.compile(r"^[ \t]*```[ \t]*([A-Za-z0-9_+.#-]*)[ \t]*$")
_SEARCH = re.compile(r"^[ \t]*<{5,9}[ \t]*SEARCH[ \t]*$")
_DIVIDER = re.compile(r"^[ \t]*={5,9}[ \t]*$")
_REPLACE = re.compile(r"^[ \t]*>{5,9}[ \t]*(REPLACE)?[ \t]*$")


class ProtocolError(Exception):
    """Antwort des Modells entspricht nicht dem Format; wird dem Modell zurückgemeldet."""


@dataclass
class FileRef:
    path: str
    start: int | None = None
    end: int | None = None


@dataclass
class EditBlock:
    """SEARCH/REPLACE-Block aus der Antwort (für kleine Modelle leichter als ein Unified Diff)."""
    path: str | None
    search: list[str]
    replace: list[str]


@dataclass
class Action:
    kind: str  # list | read | search | symbols | fetch | debug | finish
    reasoning: str
    path: str = "."
    depth: int = 3
    files: list[FileRef] = field(default_factory=list)
    query: str = ""
    reason: str = ""
    url: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    edits: list[EditBlock] = field(default_factory=list)  # SEARCH/REPLACE-Blöcke vor dem JSON
    diff_block: str = ""  # ```diff-Block außerhalb des JSON (Ersatz, wenn das Patchfeld fehlt)
    debug: dict[str, Any] = field(default_factory=dict)  # M9: Befehl und Parameter der Debug-Sitzung


@dataclass
class Fence:
    lang: str
    body: str
    start: int  # Zeichenposition der öffnenden Zeile
    end: int  # Zeichenposition nach der schließenden Zeile


def fences(text: str) -> list[Fence]:
    """Codeblöcke zeilenweise: ```lang öffnet, eine Zeile nur aus ``` schließt (verschachtelt nichts)."""
    result: list[Fence] = []
    pos = 0
    open_lang: str | None = None
    open_start = body_start = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE_LINE.match(line.rstrip("\r\n"))
        if match:
            if open_lang is None:
                open_lang, open_start, body_start = match.group(1).lower(), pos, pos + len(line)
            elif not match.group(1):
                result.append(Fence(open_lang, text[body_start:pos], open_start, pos + len(line)))
                open_lang = None
            else:
                # Neuer Block ohne Abschluss des alten: alten verwerfen.
                open_lang, open_start, body_start = match.group(1).lower(), pos, pos + len(line)
        pos += len(line)
    if open_lang is not None and open_lang == "json":
        # Fehlender Abschluss am Ende (Ausgabelimit oder Modell vergisst ```): Rest gilt als Block.
        result.append(Fence(open_lang, text[body_start:], open_start, len(text)))
    return result


def _last_json_object(text: str) -> tuple[str, str]:
    """Liefert (JSON-Text, Begründung davor). Bevorzugt den letzten ```json-Block."""
    candidates = [f for f in fences(text) if f.lang == "json" or (not f.lang and f.body.lstrip().startswith("{"))]
    if candidates:
        last = candidates[-1]
        return last.body.strip(), text[:last.start]
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


_BARE_KEY = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)')
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_comments(raw: str) -> str:
    """Entfernt // … bis Zeilenende außerhalb von Zeichenketten."""
    out: list[str] = []
    i, n = 0, len(raw)
    quote: str | None = None
    while i < n:
        ch = raw[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "/" and raw.startswith("//", i):
            while i < n and raw[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def loads_lenient(raw: str) -> Any:
    """JSON lesen; kleine Modelle schreiben oft fast-JSON (Zeilenumbrüche in Strings, einfache Anführungszeichen,
    Schlüssel ohne Anführungszeichen, Komma am Ende, Kommentare). Nur Daten, nie ausgeführt."""
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError as first:
        error = first
    repaired = _TRAILING_COMMA.sub(r"\1", _strip_comments(raw))
    try:
        return json.loads(repaired, strict=False)
    except json.JSONDecodeError:
        pass
    quoted = _BARE_KEY.sub(r'\1"\2"\3', repaired)
    try:
        return json.loads(quoted, strict=False)
    except json.JSONDecodeError:
        pass
    # Python-Literal (einfache Anführungszeichen); true/false/null außerhalb von Strings übersetzen.
    try:
        tokens = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", quoted)))
        if len(tokens) <= 200_000:
            value = ast.literal_eval(tokens)
            if isinstance(value, dict):
                return value
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    raise error


def parse(text: str) -> Action:
    raw, reasoning = _last_json_object(text)
    try:
        data = loads_lenient(raw)
    except json.JSONDecodeError as err:
        raise ProtocolError(f"The JSON block is invalid ({err.msg} at position {err.pos}). Use double quotes for all keys "
                            "and strings and escape line breaks inside strings as \\n. Send it again.") from err
    if not isinstance(data, dict):
        raise ProtocolError("The JSON block must be an object.")
    action = _action(data, reasoning.strip())
    if action.kind == "finish":
        action.edits = parse_edit_blocks(text)
        diffs = [f.body for f in fences(text) if f.lang in ("diff", "patch")]
        action.diff_block = diffs[-1] if diffs else ""
    return action


def _action(data: dict[str, Any], reasoning: str) -> Action:
    kind = data.get("action")
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
    if kind == "symbols":
        query = data.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ProtocolError("symbols needs a non-empty query (a class, function or method name).")
        return Action("symbols", reasoning, query=query.strip(), reason=str(data.get("reason", "")))
    if kind == "fetch":
        url = data.get("url")
        if not isinstance(url, str) or not url.strip().lower().startswith("https://") or len(url) > 2048:
            raise ProtocolError('fetch needs "url": "https://..." (one public web page, at most 2048 characters).')
        return Action("fetch", reasoning, url=url.strip().split("#", 1)[0], reason=str(data.get("reason", "")))
    if kind == "debug":
        command = data.get("command")
        if command not in DEBUG_COMMANDS:
            raise ProtocolError(f"debug needs \"command\": one of {', '.join(sorted(DEBUG_COMMANDS))}.")
        params = {k: v for k, v in data.items() if k in DEBUG_PARAMS}
        unknown = sorted(set(data) - DEBUG_PARAMS - {"action", "command", "reason"})
        if unknown:
            raise ProtocolError(f"debug: unknown fields {', '.join(unknown)}.")
        return Action("debug", reasoning, reason=str(data.get("reason", "")), debug={"command": command, **params})
    if kind == "finish":
        result = data.get("result")
        if not isinstance(result, dict):
            raise ProtocolError('finish needs "result": { ... } as described for your role.')
        return Action("finish", reasoning, result=result)
    raise ProtocolError('Unknown action. Use "list", "read", "search", "symbols", "fetch", "debug" (if available) or "finish". '
                        'A final result must be wrapped: {"action": "finish", "result": { ... }}.')


# M9: Befehle der Debug-Sitzung (boot = Sitzung auf dem aktuellen Stand (neu) starten).
DEBUG_COMMANDS = {"boot", "continue", "pause", "step", "break", "registers", "info", "memory", "disassemble", "screenshot",
                  "screen-text", "keys", "mouse", "serial", "reset", "events"}
DEBUG_PARAMS = {"address", "count", "length", "physical", "kind", "remove", "waitMs", "keys", "text", "dx", "dy", "button",
                "topic", "mode", "halt"}


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


_HEADERS_HINT = ("must be a unified diff with file headers: '--- a/<path>' (or '--- /dev/null' for a new file), "
                 "'+++ b/<path>', then hunks starting with '@@ -start,count +start,count @@'. Easier: use SEARCH/REPLACE "
                 "edit blocks before the JSON instead of a diff.")
_NUMBERED_HUNK = re.compile(r"^@@\s*-(\d+)")


def normalize_diff(text: str, known_files: list[str] | None = None) -> str:
    """Bringt typische Abweichungen kleiner Modelle in die kanonische Form (a/ b/-Präfixe, fehlende Kopfzeilen bei
    genau einer bekannten Datei, Hunk-Köpfe ohne Zahlen). Anwenden und prüfen tun Broker und Sandbox."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [ln for ln in lines if not _FENCE_LINE.match(ln)]
    out: list[str] = []
    has_header = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            out.append(_canonical_header(line[4:], "--- ", "a/"))
            out.append(_canonical_header(lines[i + 1][4:], "+++ ", "b/"))
            has_header = True
            i += 2
            continue
        if line.startswith("@@") and not re.match(r"^@@.*@@", line):
            number = _NUMBERED_HUNK.match(line)
            n = number.group(1) if number else "1"
            line = f"@@ -{n} +{n} @@"
        out.append(line)
        i += 1
    if not has_header and any(ln.startswith("@@") for ln in out):
        files = [f for f in (known_files or []) if f]
        if len(set(files)) != 1:
            raise ProtocolError("The diff " + _HEADERS_HINT)
        first = next(k for k, ln in enumerate(out) if ln.startswith("@@"))
        out = [f"--- a/{files[0]}", f"+++ b/{files[0]}"] + out[first:]
    return "\n".join(out).strip("\n") + "\n"


def _canonical_header(raw: str, marker: str, prefix: str) -> str:
    value = raw.split("\t", 1)[0].strip().strip('"').replace("\\", "/")
    if value == "/dev/null":
        return marker + value
    while value.startswith("./"):
        value = value[2:]
    if not value.startswith(prefix):
        value = prefix + value.lstrip("/")
    return marker + value


def check_diff(text: str, field: str) -> None:
    """Formprüfung eines Unified Diffs mit dem Leser der Sandbox (Patch.Apply prüft und wendet erneut an)."""
    try:
        patching.parse(text)
    except patching.PatchError as err:
        raise ProtocolError(f"result.{field} is not a usable diff ({err}); it {_HEADERS_HINT}") from err


# ------------------------------------------------------------------ SEARCH/REPLACE-Blöcke

def _block_path(lines: list[str], index: int) -> str | None:
    """Pfad in der Zeile vor dem Block (Codezaun, Leerzeilen und Auszeichnung übersprungen)."""
    j = index - 1
    while j >= 0 and (not lines[j].strip() or _FENCE_LINE.match(lines[j])):
        j -= 1
    if j < 0:
        return None
    value = lines[j].strip()
    value = re.sub(r"^(#+\s*|[-*]\s+)", "", value)
    value = re.sub(r"^(file|path|datei)\s*:\s*", "", value, flags=re.I)
    value = value.strip("`*: ").replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value or " " in value or len(value) > 300 or not re.search(r"[./]", value) or _REPLACE.match(lines[j]):
        return None
    return value


def parse_edit_blocks(text: str) -> list[EditBlock]:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[EditBlock] = []
    i = 0
    while i < len(lines):
        if not _SEARCH.match(lines[i]):
            i += 1
            continue
        path = _block_path(lines, i)
        j = i + 1
        search: list[str] = []
        while j < len(lines) and not _DIVIDER.match(lines[j]):
            if _SEARCH.match(lines[j]) or _REPLACE.match(lines[j]):
                raise ProtocolError("An edit block needs the three markers in this order: <<<<<<< SEARCH, =======, "
                                    ">>>>>>> REPLACE.")
            search.append(lines[j])
            j += 1
        k = j + 1
        replace: list[str] = []
        while k < len(lines) and not _REPLACE.match(lines[k]):
            if _SEARCH.match(lines[k]):
                break
            replace.append(lines[k])
            k += 1
        if j >= len(lines) or k >= len(lines) or not _REPLACE.match(lines[k]):
            raise ProtocolError("An edit block is incomplete. Each block: the file path on its own line, then "
                                "<<<<<<< SEARCH, the exact current lines, =======, the new lines, >>>>>>> REPLACE.")
        blocks.append(EditBlock(path, search, replace))
        i = k + 1
    return blocks


def _trim_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


FileLookup = Callable[[str], "list[str] | None"]


def edits_to_diff(blocks: list[EditBlock], known_files: list[str] | None = None, lookup: FileLookup | None = None) -> str:
    """SEARCH/REPLACE-Blöcke → Unified Diff. Ist der aktuelle Dateiinhalt bekannt (ganze Datei im Kontext, gleicher
    Stand), wird die Stelle gleich hier gesucht: passt SEARCH nicht oder mehrdeutig, bekommt das Modell die Rückmeldung
    sofort – ohne Freigaberunde. Sonst sucht die Sandbox die Stelle (Zeilennummern im Diff sind dann nur Hinweise)."""
    files = [f for f in (known_files or []) if f]
    order: list[str] = []
    per_file: dict[str, list[EditBlock]] = {}
    for block in blocks:
        path = block.path or (files[0] if len(set(files)) == 1 else None)
        if not path:
            raise ProtocolError("Put the file path on its own line directly before <<<<<<< SEARCH.")
        if path not in per_file:
            per_file[path] = []
            order.append(path)
        per_file[path].append(block)
    out: list[str] = []
    for path in order:
        items = per_file[path]
        creates = [b for b in items if not _trim_blank(b.search)]
        if creates:
            if len(items) != 1:
                raise ProtocolError(f"{path}: an empty SEARCH creates a new file; send exactly one block for a new file.")
            content = _trim_blank(creates[0].replace)
            if lookup is not None and lookup(path) is not None:
                raise ProtocolError(f"{path} already exists: SEARCH must contain the lines you want to replace.")
            out += ["--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(content)} @@"] + ["+" + ln for ln in content]
            continue
        current = lookup(path) if lookup is not None else None
        hunks: list[tuple[int, list[str], list[str]]] = []
        for number, block in enumerate(items, start=1):
            search = _trim_blank(block.search)
            replace = block.replace
            # Gemeinsame Leerzeilen am Rand nicht doppelt einfügen.
            if block.search and not block.search[0].strip() and replace and not replace[0].strip():
                replace = replace[1:]
            if block.search and not block.search[-1].strip() and replace and not replace[-1].strip():
                replace = replace[:-1]
            if search == replace:
                raise ProtocolError(f"{path}: SEARCH and REPLACE of block {number} are identical – nothing changes.")
            position = 0
            if current is not None:
                hits = _locate(current, search)
                if not hits:
                    preview = "\n".join(search[:3])
                    raise ProtocolError(f"{path}: the SEARCH lines of block {number} do not occur in the current file "
                                        f"(starting with:\n{preview}\n). Copy them exactly from the file content in the "
                                        "evidence; do not read the file again.")
                if len(hits) > 1:
                    raise ProtocolError(f"{path}: the SEARCH lines of block {number} occur {len(hits)} times; add "
                                        "neighbouring lines so they are unique.")
                position = hits[0]
            hunks.append((position, search, replace))
        out += [f"--- a/{path}", f"+++ b/{path}"]
        shift = 0
        for position, search, replace in hunks:
            body: list[str] = []
            for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, search, replace, autojunk=False).get_opcodes():
                if tag == "equal":
                    body += [" " + ln for ln in search[a1:a2]]
                else:
                    body += ["-" + ln for ln in search[a1:a2]] + ["+" + ln for ln in replace[b1:b2]]
            start = position + 1
            out.append(f"@@ -{start},{len(search)} +{start + shift},{len(replace)} @@")
            out += body
            shift += len(replace) - len(search)
    return "\n".join(out) + "\n"


def _locate(lines: list[str], needle: list[str]) -> list[int]:
    """Fundstellen wie in der Sandbox: exakt, sonst ohne Leerzeichen am Ende, sonst ohne Einrückung."""
    for norm in (lambda x: x, str.rstrip, str.strip):
        want = [norm(n) for n in needle]
        hits = [i for i in range(len(lines) - len(want) + 1)
                if all(norm(lines[i + k]) == want[k] for k in range(len(want)))]
        if hits:
            return hits
    return []


def resolve_patch(role: str, action: Action, known_files: list[str] | None = None,
                  lookup: FileLookup | None = None) -> None:
    """Setzt das Patchfeld der Rolle: aus SEARCH/REPLACE-Blöcken, sonst aus dem Feld bzw. einem ```diff-Block."""
    field_name = PATCH_FIELD.get(role)
    if field_name is None:
        return
    result = action.result
    files = list(known_files or [])
    listed = result.get("files")
    if isinstance(listed, list) and len(listed) == 1 and isinstance(listed[0], str):
        files = [listed[0].strip().replace("\\", "/")]
    if action.edits:
        result[field_name] = edits_to_diff(action.edits, files, lookup)
        return
    patch = result.get(field_name)
    if not isinstance(patch, str) or not patch.strip():
        patch = action.diff_block
    if isinstance(patch, str) and patch.strip():
        result[field_name] = normalize_diff(patch, files)


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
            if t.get("role") not in ("Coder", "Tester", "Debugger"):
                raise ProtocolError('task.role must be "Coder", "Tester" or "Debugger" (Debugger only for OS debugging).')
            t["reads"] = _str_list(t.get("reads"), "task.reads")
            t["writes"] = _str_list(t.get("writes"), "task.writes")
            t["depends_on"] = _str_list(t.get("depends_on"), "task.depends_on")
        result["acceptance"] = _str_list(result.get("acceptance"), "result.acceptance")
        verify = result.get("verify") or []
        if not isinstance(verify, list) or not all(
                isinstance(v, dict) and isinstance(v.get("toolchain"), str) and isinstance(v.get("action"), str)
                and isinstance(v.get("parameters", {}), dict) for v in verify):
            raise ProtocolError('result.verify must be a list of {"toolchain": "...", "action": "test", "parameters": {...}}.')
        result["verify"] = verify
    elif role == "Coder":
        patch = result.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ProtocolError("No change found: put SEARCH/REPLACE edit blocks (file path line, <<<<<<< SEARCH, "
                                "exact current lines, =======, new lines, >>>>>>> REPLACE) before the JSON.")
        check_diff(patch, "patch")
        result["files"] = _str_list(result.get("files"), "result.files")
    elif role == "Tester":
        plan = result.get("test_plan")
        if not isinstance(plan, list) or not plan:
            raise ProtocolError("result.test_plan must be a non-empty list.")
        if _str(result, "test_patch", required=False).strip():
            check_diff(result["test_patch"], "test_patch")
        # Ob Tests liefen, entscheidet allein der Testlauf in der Sandbox – Behauptungen des Modells zählen nicht.
        result.pop("executed", None)
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
