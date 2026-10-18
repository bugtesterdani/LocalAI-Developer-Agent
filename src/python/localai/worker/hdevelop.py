"""Prüfung von HDevelop-Dateien nach einem Patch (B20).

``.hdev`` (Programm), ``.hdvp`` (externe Prozedur) und ``.hdpl`` (Prozedurbibliothek) sind XML. Ein Patch, der eine
solche Datei unlesbar macht, wird gar nicht erst angewendet (alles oder nichts) – das Modell bekommt die Stelle
zurück. Die inhaltliche Prüfung (Laden mit HDevEngine) macht die Toolchain ``hdevelop`` in der VM.

Nur Standardbibliothek. DOCTYPE/ENTITY-Deklarationen werden abgelehnt (HDevelop schreibt sie nie; so gibt es keine
Entitätstricks im Worker).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

EXTENSIONS = (".hdev", ".hdvp", ".hdpl")
MAX_BYTES = 8 * 1024 * 1024
_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def is_hdevelop(path: str) -> bool:
    return path.lower().endswith(EXTENSIONS)


def check(path: str, data: bytes) -> str | None:
    """Liefert eine deutsche Problembeschreibung oder None, wenn die Datei als HDevelop-XML in Ordnung ist."""
    if not is_hdevelop(path):
        return None
    if len(data) > MAX_BYTES:
        return f"zu groß für eine HDevelop-Datei ({len(data)} Bytes)"
    if _DECLARATION.search(data):
        return "DOCTYPE/ENTITY-Deklarationen sind in HDevelop-Dateien nicht erlaubt"
    try:
        root = ET.fromstring(data)
    except ET.ParseError as err:
        line, column = getattr(err, "position", (0, 0))
        return (f"kein gültiges XML mehr (Zeile {line}, Spalte {column + 1}). In <l>…</l> müssen &, < und > als "
                "&amp; &lt; &gt; geschrieben werden")
    if root.tag != "hdevelop":
        return f"Wurzelelement muss <hdevelop> sein, gefunden <{root.tag}>"
    procedures = [p for p in root if p.tag == "procedure"]
    if not procedures:
        return "keine <procedure> enthalten"
    names: set[str] = set()
    for proc in procedures:
        name = (proc.get("name") or "").strip()
        if not name:
            return "eine <procedure> hat keinen Namen"
        if name in names:
            return f"Prozedur {name} kommt doppelt vor"
        names.add(name)
        if proc.find("body") is None:
            return f"Prozedur {name} hat keinen <body>"
    if path.lower().endswith(".hdev") and "main" not in names:
        return "Programm ohne Prozedur main"
    if path.lower().endswith(".hdvp") and len(procedures) != 1:
        return "eine .hdvp-Datei enthält genau eine Prozedur"
    return None
