"""Tool-Adapter in der Sandbox. Jeder Adapter arbeitet nur auf einem importierten Snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from localai import canonical
from localai.contracts import ResultStatus, SandboxJob, TargetResult, ToolResult
from localai.pathrules import ROOT, PathRuleError, is_within, normalize_relative
from localai.worker import patching
from localai.worker.execution import ACTIONS, ExecError, Executor
from localai.worker.repoindex import DEFAULT_MAX_CHARS, MAX_CHARS, RepoIndexer, build_map
from localai.worker.snapshot import SnapshotError, SnapshotIndex, SnapshotStore

MAX_SINGLE_READ = 256 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_SEARCH_FILE = 1024 * 1024
MAX_LIST_ENTRIES = 5_000


class ToolError(Exception):
    """Fehler bei der Ausführung (wird als Failed gemeldet)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _target_path(path: str, allow_root: bool = False) -> str:
    try:
        return normalize_relative(path, allow_root=allow_root)
    except PathRuleError as err:
        raise ToolError(f"Unzulässiger Pfad {path!r}: {err}") from err


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolError(f"{name} muss zwischen {low} und {high} liegen")
    return value


Handler = Callable[[SandboxJob, SnapshotIndex], tuple[list[TargetResult], bool]]


class ToolRunner:
    def __init__(self, store: SnapshotStore, executor: Executor | None = None, indexer: RepoIndexer | None = None) -> None:
        self.store = store
        self.executor = executor
        self.indexer = indexer
        self._exit_code: int | None = None
        self._tools: dict[str, Handler] = {
            "File.Read": self._read,
            "File.ReadMany": self._read,
            "File.List": self._list,
            "File.Search": self._search,
            "Patch.Apply": self._patch,
            "Repo.Index": self._index,
            **{name: self._toolchain for name in ACTIONS},
        }

    def run(self, job: SandboxJob, payload: Any) -> ToolResult:
        proposal = job.proposal
        self._exit_code = None
        try:
            if proposal.tool == "Workspace.Snapshot":
                results, truncated = self._import(job, payload)
            else:
                handler = self._tools.get(proposal.tool)
                if handler is None:
                    raise ToolError(f"Tool in der Sandbox nicht verfügbar: {proposal.tool}")
                grant_id = proposal.arguments.get("grantId")
                if not isinstance(grant_id, str):
                    raise ToolError("grantId fehlt")
                index = self.store.get(proposal.snapshot_id, grant_id)
                results, truncated = handler(job, index)
            status = (ResultStatus.Succeeded if all(r.status == ResultStatus.Succeeded for r in results)
                      else ResultStatus.Failed)
        except (ToolError, SnapshotError, ExecError, patching.PatchError) as err:
            results, truncated, status = [TargetResult(target_id="*", status=ResultStatus.Failed, message=str(err))], False, ResultStatus.Failed
        output = canonical.sha256_hex([r.content_sha256 or "" for r in results])
        exit_code = self._exit_code if self._exit_code is not None else (0 if status == ResultStatus.Succeeded else 1)
        return ToolResult(
            invocation_id=job.invocation_id, proposal_id=proposal.proposal_id, status=status,
            snapshot_id=proposal.snapshot_id, target_results=results, output_sha256=output,
            truncated=truncated, untrusted=True, exit_code=exit_code,
        )

    # ------------------------------------------------------------------ Snapshot
    def _import(self, job: SandboxJob, archive: Any) -> tuple[list[TargetResult], bool]:
        if job.snapshot is None or archive is None:
            raise ToolError("Snapshot-Manifest oder Archiv fehlt")
        if job.snapshot.grant_id != job.proposal.arguments.get("grantId"):
            raise ToolError("Snapshot gehört nicht zum freigegebenen Grant")
        index = self.store.import_archive(job.snapshot, archive)
        total = sum(size for size, _ in index.files.values())
        skipped = len(job.snapshot.skipped or [])
        message = f"{len(index.files)} Dateien, {total} Bytes, {skipped} übersprungen"
        return [TargetResult(target_id="snapshot", status=ResultStatus.Succeeded, text=index.snapshot_id,
                             content_sha256=index.snapshot_id, message=message)], False

    # ------------------------------------------------------------------ Lesen
    def _read(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        single = job.proposal.tool == "File.Read"
        limit = MAX_SINGLE_READ if single else MAX_BATCH_BYTES
        budget = _int_arg(job.proposal.arguments, "maxBytes", limit, 1, limit)
        results: list[TargetResult] = []
        truncated = False
        for target in job.proposal.targets:
            try:
                rel = _target_path(target.path)
                size, sha = index.files[rel] if rel in index.files else (None, None)
                if size is None:
                    raise ToolError("Datei nicht im Snapshot")
                if target.expected_sha256 is not None and target.expected_sha256 != sha:
                    raise ToolError("Datei-Hash weicht vom erwarteten Hash ab – neuer Vorschlag nötig")
                data = index.resolve(rel).read_bytes()
                text = _decode(data)
                if target.line_start is not None or target.line_end is not None:
                    lines = text.splitlines(keepends=True)
                    start = (target.line_start or 1) - 1
                    end = target.line_end or len(lines)
                    text = "".join(lines[start:end])
                encoded = text.encode("utf-8")
                cut = len(encoded) > budget
                if cut:
                    text = encoded[:budget].decode("utf-8", errors="ignore")
                    truncated = True
                budget -= min(len(encoded), budget)
                results.append(TargetResult(
                    target_id=target.target_id, status=ResultStatus.Succeeded, text=text, content_sha256=_sha(text),
                    message=f"Datei-SHA-256 {sha}" + (" (gekürzt)" if cut else "")))
            except (ToolError, SnapshotError) as err:
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Failed, message=str(err)))
        return results, truncated

    # ------------------------------------------------------------------ Auflisten
    def _list(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        depth = _int_arg(job.proposal.arguments, "depth", 2, 0, 10)
        results: list[TargetResult] = []
        truncated = False
        for target in job.proposal.targets:
            try:
                prefix = _target_path(target.path, allow_root=True)
                base_depth = 0 if prefix == ROOT else prefix.count("/") + 1
                entries: set[str] = set()
                for path, (size, _) in sorted(index.files.items()):
                    if not is_within(path, prefix) or path == prefix:
                        continue
                    parts = path.split("/")
                    rel_depth = len(parts) - base_depth
                    if rel_depth <= depth + 1:
                        entries.add(f"{path}  ({size} Bytes)")
                    else:
                        entries.add("/".join(parts[: base_depth + depth + 1]) + "/ …")
                lines = sorted(entries)
                if len(lines) > MAX_LIST_ENTRIES:
                    lines, truncated = lines[:MAX_LIST_ENTRIES], True
                if not lines and prefix != ROOT and prefix not in index.files:
                    raise ToolError("Ordner nicht im Snapshot")
                text = "\n".join(lines)
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Succeeded,
                                            text=text, content_sha256=_sha(text)))
            except ToolError as err:
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Failed, message=str(err)))
        return results, truncated

    # ------------------------------------------------------------------ Suchen
    def _search(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        args = job.proposal.arguments
        query = args.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 200:
            raise ToolError("query muss 1 bis 200 Zeichen lang sein")
        max_results = _int_arg(args, "maxResults", 200, 1, 2000)
        needle = query.casefold()
        results: list[TargetResult] = []
        truncated = False
        for target in job.proposal.targets:
            try:
                prefix = _target_path(target.path, allow_root=True)
                hits: list[str] = []
                for path, (size, _) in sorted(index.files.items()):
                    if not is_within(path, prefix) or size > MAX_SEARCH_FILE:
                        continue
                    for number, line in enumerate(_decode(index.resolve(path).read_bytes()).splitlines(), start=1):
                        if needle in line.casefold():
                            hits.append(f"{path}:{number}: {line.strip()[:300]}")
                            if len(hits) >= max_results:
                                truncated = True
                                break
                    if len(hits) >= max_results:
                        break
                text = "\n".join(hits)
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Succeeded,
                                            text=text, content_sha256=_sha(text), message=f"{len(hits)} Treffer"))
            except ToolError as err:
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Failed, message=str(err)))
        return results, truncated

    # ------------------------------------------------------------------ Patch.Apply
    def _patch(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        """Wendet die ausgewählten Dateien des Patches an und legt einen abgeleiteten Snapshot an (alles oder nichts)."""
        text = job.proposal.arguments.get("patch")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("patch fehlt")
        by_path = {f.path: f for f in patching.parse(text)}
        folded: dict[str, list[str]] = {}
        for existing in index.files:
            folded.setdefault(existing.casefold(), []).append(existing)
        results: list[TargetResult] = []
        changes: dict[str, bytes | None] = {}
        ok = True
        for target in job.proposal.targets:
            try:
                rel = _target_path(target.path)
                fp = by_path.get(rel)
                if fp is None:
                    raise ToolError("Datei kommt im Patch nicht vor")
                matches = folded.get(rel.casefold(), [])
                actual = rel if rel in index.files else (matches[0] if len(matches) == 1 else None)
                if fp.kind == "create":
                    if actual is not None:
                        raise ToolError("Datei existiert bereits – als Änderung ('--- a/…') schreiben")
                    if any(rel.casefold().startswith(p.casefold() + "/") for p in index.files):
                        raise ToolError("Ein Teil des Pfads ist eine Datei")
                    data = patching.new_file(fp).encode()
                    changes[rel] = data
                    message = f"neu angelegt ({len(data)} Bytes)"
                else:
                    if actual is None:
                        raise ToolError("Datei nicht im Snapshot – für neue Dateien '--- /dev/null' verwenden")
                    size, sha = index.files[actual]
                    if target.expected_sha256 is not None and target.expected_sha256 != sha:
                        raise ToolError("Datei-Hash weicht vom erwarteten Hash ab – neuer Vorschlag nötig")
                    if fp.kind == "delete":
                        changes[actual] = None
                        message = "gelöscht"
                    else:
                        file = patching.TextFile.decode(index.resolve(actual).read_bytes(), actual)
                        report = patching.apply_hunks(file, fp.hunks, actual)
                        data = file.encode()
                        changes[actual] = data
                        message = report.describe() + (" – Inhalt unverändert" if hashlib.sha256(data).hexdigest() == sha else "")
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Succeeded, message=message))
            except (ToolError, patching.PatchError) as err:
                ok = False
                results.append(TargetResult(target_id=target.target_id, status=ResultStatus.Failed, message=str(err)))
        if not ok:
            results.append(TargetResult(target_id="snapshot", status=ResultStatus.Failed,
                                        message="Nichts angewendet: alle ausgewählten Dateien müssen passen"))
            return results, False
        derived = self.store.derive(index, changes)
        results.append(TargetResult(target_id="snapshot", status=ResultStatus.Succeeded, text=derived.snapshot_id,
                                    content_sha256=derived.snapshot_id,
                                    message=f"abgeleitet von {index.snapshot_id[:12]}: {len(changes)} Datei(en) geändert"))
        return results, False

    # ------------------------------------------------------------------ Toolchain.*
    def _toolchain(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        if self.executor is None:
            raise ToolError("Toolchain-Aktionen sind hier nicht freigeschaltet (nur in der VM-Sandbox, nie auf dem Host)")
        result = self.executor.run(job.proposal.tool, job.job_id, dict(job.proposal.arguments), index)
        self._exit_code = result.report.exit_code
        report_text = json.dumps(result.report.to_json_obj(), ensure_ascii=False)
        command = " ".join(result.command)
        return [
            TargetResult(target_id="log", status=ResultStatus.Succeeded, text=result.log, content_sha256=_sha(result.log),
                         message=f"{result.message} · {command}"[:2000]),
            TargetResult(target_id="tests", status=ResultStatus.Succeeded, text=report_text, content_sha256=_sha(report_text),
                         message=result.message),
        ], False

    # ------------------------------------------------------------------ Repo.Index
    def _index(self, job: SandboxJob, index: SnapshotIndex) -> tuple[list[TargetResult], bool]:
        if self.indexer is None:
            raise ToolError("Repo.Index ist nicht verfügbar")
        args = job.proposal.arguments
        max_chars = _int_arg(args, "maxChars", DEFAULT_MAX_CHARS, 1000, MAX_CHARS)
        query = args.get("query")
        if query is not None:
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise ToolError("query muss 1 bis 200 Zeichen lang sein")
            hits = self.indexer.search(index, query)
            text = "\n".join(hits)[:max_chars]
            return [TargetResult(target_id="map", status=ResultStatus.Succeeded, text=text, content_sha256=_sha(text),
                                 message=f"{len(hits)} Symbole zu '{query}'")], False
        infos = self.indexer.build(index)
        text = build_map(index, infos, max_chars)
        symbols = sum(len(i.symbols) for i in infos)
        return [TargetResult(target_id="map", status=ResultStatus.Succeeded, text=text, content_sha256=_sha(text),
                             message=f"{len(index.files)} Dateien, {len(infos)} Quelldateien, {symbols} Symbole")], False
