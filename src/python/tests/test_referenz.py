"""Nachschlageordner (references): HTML/Text/CHM in der Sandbox durchsuchen und lesen, Aktion docs."""

from __future__ import annotations

import pathlib
import shutil

import pytest

from localai.contracts import ResultStatus, ToolTarget
from localai.orchestrator import protocol
from localai.worker import reference
from localai.worker.reference import ReferenceLibrary, ReferenceLookupError, html_to_text
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner

from tests.test_worker import job, make_snapshot, proposal

PAGE = (b"<HTML><HEAD><TITLE>win32file.CreateFile</TITLE><SCRIPT>alert('x')</SCRIPT>\n"
        b"<H1>CreateFile</H1><P>Creates or opens a file.<TABLE><TR><TD>GENERIC_READ</TD><TD>read access</TD></TR></TABLE>")
FILES = {
    "api/createfile.htm": PAGE,
    "api/closehandle.html": b"<html><head><title>CloseHandle</title></head><body><p>Closes an open handle.</p></body></html>",
    "notes/fehler.md": "# Fehlercodes\n\nERROR_ACCESS_DENIED (5): Zugriff verweigert – CreateFile ohne Rechte.\n".encode(),
    "bild.png": b"\x89PNG",
}


def imported(tmp_path, files, grant="ref-hilfe"):
    store = SnapshotStore(tmp_path / "work")
    manifest, archive = make_snapshot(tmp_path, files, grant)
    return store, store.import_archive(manifest, archive)


def test_html_ohne_kopfende_und_ohne_skripte():
    title, text = html_to_text(PAGE)
    assert title == "win32file.CreateFile"
    assert "# CreateFile" in text and "Creates or opens a file." in text and "GENERIC_READ" in text
    assert "alert" not in text
    assert html_to_text(b'<meta charset="windows-1252"><p>Gr\xfc\xdfe</p>')[1] == "Grüße"


def test_suche_ordnet_nach_allen_woertern_und_titel(tmp_path):
    _, index = imported(tmp_path, FILES)
    lib = ReferenceLibrary(tmp_path / "cache", extractor=None)
    text = lib.search(index, "CreateFile", 10, "hilfe", "hilfe:")
    lines = text.splitlines()
    assert lines[0].startswith('Reference "hilfe": 2 pages match')
    assert lines[1].startswith("1. hilfe:api/createfile.htm – win32file.CreateFile")  # Titel zählt mehr
    assert "hilfe:notes/fehler.md" in text and "bild.png" not in text
    both = lib.search(index, "createfile zugriff", 10, "hilfe")
    assert "1 pages match" in both.splitlines()[0] and "notes/fehler.md" in both
    assert "No page matches" in lib.search(index, "gibtsnicht", 10, "hilfe")


def test_lesen_in_teilen_mit_hinweis_auf_den_naechsten(tmp_path):
    long = b"<title>Lang</title><p>" + b"Wort " * 3000 + b"</p>"
    _, index = imported(tmp_path, {"lang.htm": long})
    lib = ReferenceLibrary(tmp_path / "cache", extractor=None)
    text, more = lib.read(index, "lang.htm", 1000, 1, "hilfe:")
    assert more and text.startswith("# Lang\n(hilfe:lang.htm, part 1/15)")
    assert '{"action": "docs", "page": "hilfe:lang.htm", "part": 2}' in text
    last, more = lib.read(index, "lang.htm", 1000, 99)
    assert not more and "part 15/15" in last
    with pytest.raises(ReferenceLookupError, match="meintest du: hilfe:lang.htm"):
        lib.read(index, "x/lang.htm", 1000, 1, "hilfe:")


def _fake_chm(pages: dict[str, bytes]):
    def extract(chm: pathlib.Path, dest: pathlib.Path) -> None:
        assert chm.read_bytes().startswith(b"ITSF")
        for name, data in pages.items():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return extract


def test_chm_wird_in_der_sandbox_entpackt_und_zwischengespeichert(tmp_path):
    _, index = imported(tmp_path, {"hilfe/api.chm": b"ITSF fake", "readme.txt": b"Siehe api.chm"})
    calls = []
    fake = _fake_chm({"html/createfile.htm": PAGE, "html/a:b.htm": b"<p>unzulaessiger Name</p>", "#SYSTEM": b"x"})
    lib = ReferenceLibrary(tmp_path / "cache", extractor=lambda c, d: (calls.append(c), fake(c, d)))
    text = lib.search(index, "createfile", 5, "hilfe", "hilfe:")
    assert "hilfe:hilfe/api.chm/html/createfile.htm – win32file.CreateFile" in text
    assert "a:b" not in text  # Seitenname, der keine gültige Seitenkennung ist, wird übergangen
    page, _ = lib.read(index, "hilfe/api.chm/html/createfile.htm", 5000, 1)
    assert "GENERIC_READ" in page
    # Neuer Prozess bzw. neue Bibliothek: entpackt nicht erneut (Cache je Datei-Hash)
    ReferenceLibrary(tmp_path / "cache", extractor=lambda c, d: (calls.append(c), fake(c, d))).search(index, "x", 1, "h")
    assert len(calls) == 1


def test_chm_ohne_entpacker_wird_benannt_statt_still_uebergangen(tmp_path):
    _, index = imported(tmp_path, {"api.chm": b"ITSF", "a.htm": b"<p>x</p>"})
    text = ReferenceLibrary(tmp_path / "cache", extractor=None).search(index, "x", 5, "hilfe")
    assert "Not searched: api.chm: CHM kann hier nicht entpackt werden" in text
    broken = ReferenceLibrary(tmp_path / "cache2", extractor=lambda c, d: None).search(index, "x", 5, "hilfe")
    assert "keine HTML-Seiten" in broken


@pytest.mark.skipif(shutil.which("7z") is None, reason="7z nicht installiert")
def test_entpacker_im_linux_gast_ist_7z(monkeypatch):
    monkeypatch.setattr(reference.sys, "platform", "linux")
    assert reference.default_extractor() is not None


def test_worker_reference_search_und_read(tmp_path):
    store, index = imported(tmp_path, FILES)
    runner = ToolRunner(store, references=ReferenceLibrary(tmp_path / "cache", extractor=None))
    found = runner.run(job(proposal("Reference.Search", [], index.snapshot_id, grantId="ref-hilfe", query="CloseHandle"), 1), None)
    assert found.status == ResultStatus.Succeeded and found.target_results[0].target_id == "refs"
    assert "hilfe:api/closehandle.html – CloseHandle" in found.target_results[0].text
    read = runner.run(job(proposal("Reference.Read", [ToolTarget(target_id="t1", path="api/closehandle.html")],
                                   index.snapshot_id, grantId="ref-hilfe", maxChars=2000), 2), None)
    assert read.status == ResultStatus.Succeeded and "Closes an open handle." in read.target_results[0].text
    missing = runner.run(job(proposal("Reference.Read", [ToolTarget(target_id="t1", path="api/nix.htm")],
                                      index.snapshot_id, grantId="ref-hilfe"), 3), None)
    assert missing.status == ResultStatus.Failed and "Seite nicht gefunden" in missing.target_results[0].message


def test_aktion_docs():
    search = protocol.parse('```json\n{"action": "docs", "query": "CreateFile flags", "reference": "win32"}\n```')
    assert (search.kind, search.query, search.reference, search.page) == ("docs", "CreateFile flags", "win32", "")
    page = protocol.parse('{"action": "docs", "page": "win32:api.chm/html/x.htm", "part": 2}')
    assert (page.page, page.part) == ("win32:api.chm/html/x.htm", 2)
    with pytest.raises(protocol.ProtocolError, match="docs needs"):
        protocol.parse('{"action": "docs"}')
    with pytest.raises(protocol.ProtocolError, match="part"):
        protocol.parse('{"action": "docs", "page": "a", "part": 0}')
