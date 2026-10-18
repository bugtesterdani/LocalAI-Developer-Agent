"""Rollen-Prompts als versionierte Dateien (englisch, Spezifikation §11)."""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass

PROMPT_DIR = pathlib.Path(__file__).with_name("prompts")
_HEADER = re.compile(r"<!--\s*prompt:\s*(\w+)\s*·\s*version:\s*(\d+)\s*-->")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str
    sha256: str

    @property
    def tag(self) -> str:
        return f"{self.name} v{self.version} ({self.sha256[:12]})"


def load(name: str, directory: pathlib.Path = PROMPT_DIR) -> Prompt:
    raw = (directory / f"{name}.md").read_text(encoding="utf-8")
    first, _, body = raw.partition("\n")
    match = _HEADER.fullmatch(first.strip())
    if not match or match.group(1) != name:
        raise ValueError(f"Prompt {name}.md: Kopfzeile '<!-- prompt: {name} · version: N -->' fehlt")
    return Prompt(name, int(match.group(2)), body.strip(), hashlib.sha256(raw.encode()).hexdigest())


def system_prompt(role: str, directory: pathlib.Path = PROMPT_DIR) -> tuple[str, list[Prompt]]:
    common = load("common", directory)
    specific = load(role.lower(), directory)
    return common.text.replace("{role}", role) + "\n\n" + specific.text, [common, specific]
