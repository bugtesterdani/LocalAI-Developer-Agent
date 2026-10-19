"""Content Gate: macht aus den Rohbytes der Quarantäne inerten Text für die Inhaltsfreigabe (Spezifikation §7).

* HTML: nur sichtbarer Text; Skripte, Styles, eingebettete Objekte, Formulare, versteckte Elemente (``hidden``,
  ``aria-hidden``, ``display:none`` …) entfallen; Links werden als Text mit Ziel ausgegeben (nicht aktiv).
* Steuerzeichen und ANSI-Sequenzen werden entfernt, Bidi- und unsichtbare Zeichen sichtbar markiert (⟦U+202E⟧).
* Höchstens ``max_chars`` Zeichen (Standard 20.000), Kürzung sichtbar.
* Hinweise auf mögliche Prompt Injection werden markiert – **das ist keine Sicherheitsgarantie**; der Text bleibt
  untrusted Evidenz, die Schutzschichten sind Broker, Nutzerentscheidung und Sandbox.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from localai.web import SANITIZER_VERSION

DEFAULT_MAX_CHARS = 20_000
_SKIP = {"script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "math", "canvas", "head",
         "select", "textarea", "button", "form", "video", "audio", "map", "applet", "frame", "frameset", "dialog"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "ul", "ol", "table", "tr",
          "blockquote", "figure", "figcaption", "dl", "dt", "dd", "details", "summary", "address", "hr", "br"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])"
                           r"|(?:height|width)\s*:\s*0(?:px)?\s*(?:;|$)", re.I)
_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
_INVISIBLE = {0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180E, 0x3164, 0xFEFF, 0xFFA0}
_INJECTION = [
    (re.compile(r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|earlier)\b.{0,20}\b(instructions?|rules?|prompts?)", re.I),
     "fordert auf, Anweisungen zu ignorieren"),
    (re.compile(r"\b(ignorier|vergiss|missacht)\w*\b.{0,40}\b(anweisung|regel|vorgabe)", re.I), "fordert auf, Anweisungen zu ignorieren"),
    (re.compile(r"\b(system prompt|developer message|you are now|act as|new instructions|jailbreak)\b", re.I),
     "spricht ein KI-System direkt an"),
    (re.compile(r"\b(approve|auto-?approve|grant (all )?permissions?|freigabe|freigeben|genehmig\w*)\b", re.I),
     "erwähnt Freigaben/Genehmigungen"),
    (re.compile(r"\b(run|execute|ausführen|führe .{0,20} aus)\b.{0,30}\b(command|befehl|script|skript|shell)\b", re.I),
     "fordert zum Ausführen von Befehlen auf"),
    (re.compile(r"(\bcurl\b|\bwget\b|Invoke-WebRequest|\biwr\b|powershell\s+-|cmd(\.exe)?\s+/c|rm\s+-rf|\bdel\s+/[sq])", re.I),
     "enthält Befehle (Netz/Löschen)"),
    (re.compile(r"\b(export|upload|send|exfiltrat\w*|übertrag\w*|hochlad\w*)\b.{0,40}\b(files?|code|secrets?|keys?|dateien|quelltext|token)", re.I),
     "fordert Datenexport"),
    (re.compile(r"\b(api[_ -]?key|password|passwort|secret|private key|ssh-rsa|BEGIN [A-Z ]*PRIVATE KEY)\b", re.I),
     "erwähnt Zugangsdaten"),
]


@dataclass
class Warning_:
    kind: str  # injection | invisible | hidden | truncated | control
    message: str
    excerpt: str = ""


@dataclass
class GateResult:
    sanitizer_version: str
    text_sha256: str
    chars: int
    truncated: bool
    content_kind: str
    title: str = ""
    links: int = 0
    removed: dict[str, int] = field(default_factory=dict)
    warnings: list[Warning_] = field(default_factory=list)


class _Extractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (Tag, verborgen/übersprungen)
        self.pre = 0
        self.title = ""
        self._in_title = False
        self.links: list[str] = []
        self._href: str | None = None
        self.removed = {"scripts": 0, "hidden": 0, "embedded": 0}
        self.list_depth = 0

    @property
    def skipping(self) -> bool:
        return any(hidden for _, hidden in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        hidden = False
        if tag in _SKIP:
            hidden = True
            key = "scripts" if tag in ("script", "noscript", "template", "style") else "embedded"
            if tag != "head":
                self.removed[key] += 1
        elif ("hidden" in a or a.get("aria-hidden", "").lower() == "true" or a.get("type", "").lower() == "hidden"
              or _HIDDEN_STYLE.search(a.get("style", ""))):
            hidden = True
            self.removed["hidden"] += 1
        if tag == "title":
            self._in_title = True
        if tag not in _VOID:
            self.stack.append((tag, hidden))
        if self.skipping or hidden:
            return
        if tag == "pre":
            self.pre += 1
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n" + "  " * max(0, self.list_depth - 1) + "- ")
        elif tag in ("ul", "ol"):
            self.list_depth += 1
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in _BLOCK or tag == "pre":
            self.parts.append("\n")
        elif tag == "img" and a.get("alt"):
            self.parts.append(f"[Bild: {a['alt'][:200]}]")
        if tag == "a":
            href = a.get("href", "").strip()
            self._href = None if not href or href.lower().startswith(("javascript:", "data:", "vbscript:", "#")) else urljoin(self.base, href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _VOID or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                break
        if self.skipping:
            return
        if tag == "a" and self._href:
            self.parts.append(f" (→ {self._href})")
            self.links.append(self._href)
            self._href = None
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
        if tag in _BLOCK or tag in ("h1", "h2", "h3", "h4", "h5", "h6", "pre", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = " ".join(data.split())[:300]
        if self.skipping:
            return
        self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [ln.rstrip() for ln in raw.split("\n")]
        out: list[str] = []
        blank = 0
        for ln in lines:
            if not ln.strip():
                blank += 1
                if blank <= 1 and out:
                    out.append("")
                continue
            blank = 0
            out.append(ln if ln.startswith("  ") else ln.strip())
        return "\n".join(out).strip()


def decode(raw: bytes, charset: str, content_type: str) -> str:
    candidates = [charset] if charset else []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.insert(0, "utf-8-sig")
    if "html" in content_type and not charset:
        match = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", raw[:4096], re.I)
        if match:
            candidates.append(match.group(1).decode("ascii", "ignore"))
    for enc in [*candidates, "utf-8"]:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def clean_chars(text: str) -> tuple[str, int, int]:
    """Entfernt Steuerzeichen/ANSI, markiert Bidi-/unsichtbare Zeichen. Liefert (Text, entfernt, markiert)."""
    text, ansi = _ANSI.subn("", text)
    removed = ansi
    marked = 0
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if ch in "\n\t":
            out.append(ch)
        elif cat == "Cc":
            removed += 1
        elif cat == "Cf" or cp in _INVISIBLE or 0xE0000 <= cp <= 0xE007F or 0xFE00 <= cp <= 0xFE0F:
            out.append(f"⟦U+{cp:04X}⟧")
            marked += 1
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", "".join(out)), removed, marked


def injection_warnings(text: str) -> list[Warning_]:
    warnings: list[Warning_] = []
    for pattern, message in _INJECTION:
        for match in pattern.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            line = text[start:end if end >= 0 else len(text)].strip()
            warnings.append(Warning_("injection", message, line[:240]))
            if len(warnings) >= 20:
                return warnings
            break
    return warnings


def sanitize(raw: bytes, content_type: str, charset: str = "", base_url: str = "",
             max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, GateResult]:
    decoded = decode(raw, charset, content_type)
    links = 0
    title = ""
    removed: dict[str, int] = {}
    if "html" in content_type or (not content_type and re.search(r"<html|<body|<!doctype", decoded[:2000], re.I)):
        extractor = _Extractor(base_url)
        extractor.feed(decoded)
        extractor.close()
        text, title, links, removed = extractor.text(), extractor.title, len(extractor.links), dict(extractor.removed)
        kind = "html"
    elif "json" in content_type:
        try:
            text = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
        except ValueError:
            text = decoded
        kind = "json"
    else:
        text = decoded
        kind = "text"
    text, control, marked = clean_chars(text)
    removed["control"] = control
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n[… gekürzt: höchstens {max_chars} Zeichen werden übergeben]"
    warnings = injection_warnings(text)
    if marked:
        warnings.append(Warning_("invisible", f"{marked} unsichtbare oder Bidi-Steuerzeichen sichtbar markiert (⟦U+…⟧)"))
    if removed.get("hidden"):
        warnings.append(Warning_("hidden", f"{removed['hidden']} versteckte Element(e) entfernt – deren Text wird nicht übergeben"))
    if control:
        warnings.append(Warning_("control", f"{control} Steuerzeichen/ANSI-Sequenzen entfernt"))
    if truncated:
        warnings.append(Warning_("truncated", f"Text auf {max_chars} Zeichen gekürzt"))
    result = GateResult(SANITIZER_VERSION, hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text), truncated, kind,
                        title, links, removed, warnings)
    return text, result


def run(work: pathlib.Path, max_chars: int = DEFAULT_MAX_CHARS) -> GateResult | None:
    meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    raw_path = work / "raw.bin"
    if not raw_path.is_file():
        return None
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta.get("raw_sha256"):
        raise ValueError("Rohdaten in der Quarantäne passen nicht zum Hash des Abrufs")
    text, result = sanitize(raw, str(meta.get("content_type", "")), str(meta.get("charset", "")), str(meta.get("url", "")),
                            max_chars)
    (work / "text.txt").write_bytes(text.encode("utf-8"))
    (work / "gate.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=1), encoding="utf-8")
    return result
