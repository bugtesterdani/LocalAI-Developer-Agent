from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Kontakt:
    """Ein Kontakt; email und ort dürfen fehlen (None)."""

    name: str
    email: str | None = None
    ort: str | None = None

    @property
    def anzeige(self) -> str:
        return self.name if self.ort is None else f"{self.name} ({self.ort})"
