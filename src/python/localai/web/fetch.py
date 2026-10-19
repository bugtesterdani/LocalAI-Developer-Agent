"""Fetch-Stufe: genau ein HTTPS GET auf die geprüfte Adresse, Rohbytes in die Quarantäne (Spezifikation §7).

Keine Cookies, keine Authentifizierung, kein Proxy (Umgebungsvariablen werden ignoriert: eigene Verbindung über
``socket`` + ``ssl``), kein JavaScript, keine Subressourcen. Weiterleitungen werden angehalten und gemeldet
(neue URL = neue Freigabe). Ergebnis: ``meta.json`` und ggf. ``raw.bin`` im Ausgabeordner.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import pathlib
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

from localai.web.netcheck import UrlRejected, check_addresses, check_url

USER_AGENT = "LocalAI-WebFetch/1 (offline developer assistant; single approved request)"
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/x-markdown", "text/csv",
              "application/json", "application/xml", "text/xml", "text/x-rst", "application/ld+json")
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0

Resolver = Callable[[str], list[str]]


@dataclass
class FetchMeta:
    url: str
    host: str = ""
    address: str = ""
    status: int = 0
    content_type: str = ""
    charset: str = ""
    raw_bytes: int = 0
    raw_sha256: str = ""
    redirect: str = ""  # Ziel einer angehaltenen Weiterleitung
    error: str = ""
    fetched_at: str = ""
    seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error and not self.redirect and self.status == 200


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(info[4][0] for info in infos))


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS-Verbindung zu genau der geprüften Adresse; TLS-SNI und Zertifikatsprüfung für den Hostnamen."""

    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._address = address
        self._ctx = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), timeout=self.timeout)
        self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)


def fetch(url: str, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT,
          resolver: Resolver = system_resolver, context: ssl.SSLContext | None = None,
          port: int = 443, allow: Callable[[str], bool] | None = None) -> tuple[FetchMeta, bytes]:
    """Holt die URL. ``allow``/``port`` nur für Tests (lokaler TLS-Server); im Betrieb nie gesetzt."""
    started = time.monotonic()
    meta = FetchMeta(url=url, fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    body = b""
    try:
        checked = check_url(url)
        meta.url, meta.host = checked.url, checked.host
        if checked.literal is not None:
            address = str(checked.literal)
        else:
            try:
                answers = resolver(checked.host)
            except OSError as err:
                raise UrlRejected(f"{checked.host}: DNS-Auflösung fehlgeschlagen ({err})") from err
            if allow is not None and all(allow(a) for a in answers):
                address = answers[0]
            else:
                address = str(check_addresses(checked.host, answers))
        meta.address = address
        ctx = context or ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        deadline = started + timeout
        connection = _PinnedConnection(checked.host, address, port, ctx, timeout)
        try:
            connection.request("GET", checked.path, headers={
                "Host": checked.host, "User-Agent": USER_AGENT, "Accept": ", ".join(TEXT_TYPES) + ";q=0.9, */*;q=0.1",
                "Accept-Encoding": "identity", "Connection": "close"})
            response = connection.getresponse()
            meta.status = response.status
            meta.headers = {k.lower(): v[:300] for k, v in response.getheaders()[:40]}
            content_type = response.getheader("Content-Type", "") or ""
            meta.content_type = content_type.split(";", 1)[0].strip().lower()
            if "charset=" in content_type.lower():
                meta.charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip().strip('"\'')
            if 300 <= response.status < 400:
                location = response.getheader("Location", "")
                meta.redirect = urljoin(checked.url, location) if location else "(ohne Ziel)"
                return meta, b""
            if response.status != 200:
                meta.error = f"HTTP-Status {response.status}"
                return meta, b""
            if meta.content_type and not meta.content_type.startswith(TEXT_TYPES):
                meta.error = f"Inhaltstyp {meta.content_type} wird nicht abgerufen (nur Text/HTML/JSON/XML; keine PDFs, Archive, Binärdateien)"
                return meta, b""
            declared = response.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                meta.error = f"Antwort ist größer als {max_bytes} Bytes ({declared})"
                return meta, b""
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    meta.error = f"Zeitgrenze von {timeout:.0f} s überschritten"
                    return meta, b""
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    meta.error = f"Antwort ist größer als {max_bytes} Bytes"
                    return meta, b""
                chunks.append(chunk)
            body = b"".join(chunks)
            if not meta.content_type and b"\x00" in body[:4096]:
                meta.error = "Antwort ist keine Textdatei"
                return meta, b""
        finally:
            connection.close()
    except UrlRejected as err:
        meta.error = str(err)
    except ssl.SSLError as err:
        meta.error = f"TLS-Fehler (Zertifikat/Hostname): {err.reason or err}"
    except (OSError, http.client.HTTPException) as err:
        meta.error = f"Verbindung fehlgeschlagen: {err}"
    finally:
        meta.seconds = round(time.monotonic() - started, 2)
    meta.raw_bytes = len(body)
    meta.raw_sha256 = hashlib.sha256(body).hexdigest()
    return meta, body


def write(out: pathlib.Path, meta: FetchMeta, body: bytes) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if meta.ok:
        (out / "raw.bin").write_bytes(body)
    (out / "meta.json").write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=1), encoding="utf-8")
