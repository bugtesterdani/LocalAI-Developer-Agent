"""Formale Pfadregeln – identisch zu ``LocalAi.Broker.PathRules`` (C#).

Gemeinsame Testvektoren: ``contracts/testvectors/paths.json``.
"""

from __future__ import annotations

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
FORBIDDEN_CHARS = set(':*?"<>|')
ROOT = "."


class PathRuleError(ValueError):
    """Pfad verletzt die Regeln."""


def normalize_relative(path: str | None, allow_root: bool = False) -> str:
    """Gibt den normalisierten relativen Pfad mit '/' zurück oder wirft ``PathRuleError``."""
    if allow_root and path == ROOT:
        return ROOT
    if not path or len(path) > 1024:
        raise PathRuleError("Pfad fehlt oder ist zu lang")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise PathRuleError("Pfad enthält Steuerzeichen")
    if any(c in FORBIDDEN_CHARS for c in path):
        raise PathRuleError("Pfad enthält unzulässige Zeichen (z. B. ':' für Laufwerk oder Alternate Data Stream)")
    if path[0] in "/\\":
        raise PathRuleError("Absolute, UNC- und Gerätepfade sind nicht erlaubt")
    segments = path.replace("\\", "/").split("/")
    for segment in segments:
        if not segment:
            raise PathRuleError("Leeres Pfadsegment")
        if segment in (".", ".."):
            raise PathRuleError("'.' und '..' sind nicht erlaubt")
        if segment.endswith((".", " ")) or segment.startswith(" "):
            raise PathRuleError("Pfadsegmente dürfen nicht mit Punkt oder Leerzeichen enden")
        if segment.split(".")[0].upper() in RESERVED_NAMES:
            raise PathRuleError(f"Reservierter Gerätename: {segment}")
    return "/".join(segments)


def is_within(path: str, prefix: str) -> bool:
    """True, wenn ``path`` gleich ``prefix`` ist oder darunter liegt (beide normalisiert)."""
    if prefix == ROOT:
        return True
    return path == prefix or path.startswith(prefix + "/")
