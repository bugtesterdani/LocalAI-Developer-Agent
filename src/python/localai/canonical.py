"""Kanonisches JSON (Teilmenge von RFC 8785 / JCS) und SHA-256.

Muss byte-identisch zu ``LocalAi.Contracts.CanonicalJson`` (C#) sein.
Gemeinsame Testvektoren: ``contracts/testvectors/canonical.json``.

Regeln:
* Objektschlüssel sortiert nach UTF-16-Codeeinheiten, doppelte Schlüssel verboten.
* Nur Ganzzahlen im Bereich +-(2**53 - 1); Gleitkommazahlen, NaN, Infinity verboten.
* Strings: nur ``"`` ``\\`` und Steuerzeichen < 0x20 werden escaped
  (``\\b \\f \\n \\r \\t``, sonst ``\\u00xx`` klein), alles andere roh als UTF-8.
* Einzelne Surrogate sind verboten.
* Keine Leerzeichen.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_SAFE_INT = 2**53 - 1

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalJsonError(ValueError):
    """Wert lässt sich nicht kanonisch darstellen."""


def _reject_float(text: str) -> Any:
    raise CanonicalJsonError(f"Gleitkommazahl nicht erlaubt: {text}")


def _reject_constant(text: str) -> Any:
    raise CanonicalJsonError(f"Konstante nicht erlaubt: {text}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJsonError(f"Doppelter Schlüssel: {key}")
        result[key] = value
    return result


def parse(text: str | bytes) -> Any:
    """Strikter Parser: verbietet Gleitkommazahlen, Konstanten und doppelte Schlüssel."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return json.loads(
        text,
        parse_float=_reject_float,
        parse_constant=_reject_constant,
        object_pairs_hook=_no_duplicates,
    )


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _escape_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonicalJsonError("Einzelnes Surrogat im String")
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalJsonError(f"Ganzzahl außerhalb des sicheren Bereichs: {value}")
        out.append(str(value))
    elif isinstance(value, float):
        raise CanonicalJsonError("Gleitkommazahl nicht erlaubt")
    elif isinstance(value, str):
        out.append(_escape_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJsonError("Objektschlüssel müssen Strings sein")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_escape_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise CanonicalJsonError(f"Typ nicht unterstützt: {type(value).__name__}")


def canonicalize(value: Any) -> str:
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    return canonicalize(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
