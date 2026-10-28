"""Ersatz für toolchain-update.ps1 in Tests: „installiert“ jede Toolchain, deren toolchain.json lesbar ist.

Aufruf wie das Gastskript: ``-Names a,b -SourceRoot DIR -ResultFile DATEI``. Schlägt fehl, wenn die Toolchain eine
Datei ``FAIL`` enthält. Legt eine Kopie von toolchain.json unter ``<SourceRoot>/../installed/<name>.json`` ab.
"""

import json
import pathlib
import shutil
import sys

args = dict(zip(sys.argv[1::2], sys.argv[2::2], strict=False))
source = pathlib.Path(args["-SourceRoot"])
installed = source.parent / "installed"
installed.mkdir(exist_ok=True)
results = []
for name in args["-Names"].split(","):
    folder = source / name
    if (folder / "FAIL").exists():
        results.append({"name": name, "installed": False, "error": "Selbsttest: Ausgabe 'ok' fehlt"})
        continue
    manifest = json.loads((folder / "toolchain.json").read_text(encoding="utf-8"))
    shutil.copyfile(folder / "toolchain.json", installed / f"{name}.json")
    results.append({"name": name, "version": manifest.get("version", ""), "installed": True, "selfTest": "ok", "seconds": 1})
pathlib.Path(args["-ResultFile"]).write_text(json.dumps(results), encoding="utf-8")
