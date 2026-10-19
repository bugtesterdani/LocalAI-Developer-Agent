"""Fetch-Stufe: genau ein HTTPS GET auf die geprüfte Adresse, Rohbytes in die Quarantäne (Spezifikation §7).

Keine Cookies, keine Authentifizierung beim Zielserver, kein JavaScript, keine Subressourcen. Weiterleitungen werden
angehalten und gemeldet (neue URL = neue Freigabe). Ergebnis: ``meta.json`` und ggf. ``raw.bin`` im Ausgabeordner.

Proxy nur, wenn in ``config/localai.json`` (``web.proxy``) ausdrücklich gesetzt – Umgebungsvariablen werden nie
verwendet. Über den Proxy läuft ein CONNECT-Tunnel; TLS wird weiterhin Ende zu Ende gegen den Hostnamen geprüft.
Ein TLS-aufbrechender Firmenproxy braucht dafür sein Stammzertifikat (Windows-Zertifikatsspeicher oder ``caFile``);
der Aussteller des Serverzertifikats wird festgehalten und in der Freigabe angezeigt.
"""

from __future__ import annotations

import base64
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
from urllib.parse import urljoin, urlsplit

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
    request: str = ""  # genau die gesendete Anfrage (ohne Proxy-Zugangsdaten)
    proxy: str = ""  # host:port des Proxys oder leer (direkt)
    tls_issuer: str = ""  # Aussteller des Serverzertifikats (zeigt TLS-Aufbruch durch einen Proxy)

    @property
    def ok(self) -> bool:
        return not self.error and not self.redirect and self.status == 200


@dataclass(frozen=True)
class Proxy:
    """HTTP-Proxy für CONNECT (nur aus der Konfiguration)."""

    host: str
    port: int
    username: str = ""
    password: str = ""
    dns_via_proxy: bool = False  # Zielname nur vom Proxy auflösen lassen (wenn lokal kein externes DNS)
    connect_by_address: bool = False  # CONNECT auf die lokal geprüfte IP statt den Namen (strenger, nicht jeder Proxy erlaubt es)

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    @staticmethod
    def parse(url: str, username: str = "", password: str = "", dns_via_proxy: bool = False,
              connect_by_address: bool = False) -> Proxy:
        parts = urlsplit(url.strip())
        if (parts.scheme != "http" or not parts.hostname or parts.username or parts.path not in ("", "/") or parts.query
                or parts.fragment or parts.port is None):
            raise ValueError("web.proxy.url muss die Form http://host:port haben, mit Port (Zugangsdaten getrennt angeben)")
        return Proxy(parts.hostname, parts.port, username, password, dns_via_proxy, connect_by_address)


def tls_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Systemspeicher (unter Windows inkl. Firmen-Stammzertifikaten) plus optional eine PEM-Datei."""
    ctx = ssl.create_default_context()
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _issuer(sock: object) -> str:
    try:
        cert = sock.getpeercert()  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return ""
    if not cert:
        return ""
    fields = {k: v for rdn in cert.get("issuer", ()) for k, v in rdn}
    return ", ".join(f"{k}={fields[k]}" for k in ("commonName", "organizationName") if k in fields)[:200]


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


def request_headers(host: str) -> dict[str, str]:
    return {"Host": host, "User-Agent": USER_AGENT, "Accept": ", ".join(TEXT_TYPES) + ";q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity", "Connection": "close"}


def request_text(host: str, path: str) -> str:
    return "\n".join([f"GET {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in request_headers(host).items()])


class _TunnelConnection(http.client.HTTPSConnection):
    """CONNECT-Tunnel über den konfigurierten Proxy; TLS (SNI, Zertifikat) immer gegen den Hostnamen der URL."""

    def __init__(self, proxy: Proxy, target: str, tls_host: str, port: int, context: ssl.SSLContext, timeout: float,
                 headers: dict[str, str]) -> None:
        super().__init__(proxy.host, proxy.port, timeout=timeout, context=context)
        self.set_tunnel(target, port, headers=headers)
        self._tls_host = tls_host
        self._ctx = context

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)  # TCP zum Proxy + CONNECT
        self.sock = self._ctx.wrap_socket(self.sock, server_hostname=self._tls_host)


def fetch(url: str, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT,
          resolver: Resolver = system_resolver, context: ssl.SSLContext | None = None,
          port: int = 443, allow: Callable[[str], bool] | None = None,
          proxy: Proxy | None = None) -> tuple[FetchMeta, bytes]:
    """Holt die URL. ``allow``/``port`` nur für Tests (lokaler TLS-Server); im Betrieb nie gesetzt."""
    started = time.monotonic()
    meta = FetchMeta(url=url, fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    body = b""
    try:
        checked = check_url(url)
        meta.url, meta.host = checked.url, checked.host
        address = ""
        if checked.literal is not None:
            address = str(checked.literal)
        else:
            try:
                answers = resolver(checked.host)
            except OSError as err:
                if proxy is None or not proxy.dns_via_proxy:
                    raise UrlRejected(f"{checked.host}: DNS-Auflösung fehlgeschlagen ({err})") from err
                answers = []  # nur der Proxy löst auf (web.proxy.dnsViaProxy)
            if answers:
                if allow is not None and all(allow(a) for a in answers):
                    address = answers[0]
                else:
                    address = str(check_addresses(checked.host, answers))
        meta.address = address or "(Auflösung durch den Proxy)"
        ctx = context or tls_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        deadline = started + timeout
        headers = request_headers(checked.host)
        meta.request = request_text(checked.host, checked.path)
        if proxy is None:
            connection: http.client.HTTPSConnection = _PinnedConnection(checked.host, address, port, ctx, timeout)
        else:
            meta.proxy = proxy.label
            tunnel_headers = {}
            if proxy.username:
                token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode("ascii")
                tunnel_headers["Proxy-Authorization"] = f"Basic {token}"
            target = address if proxy.connect_by_address and address else checked.host
            connection = _TunnelConnection(proxy, target, checked.host, port, ctx, timeout, tunnel_headers)
            meta.request = f"CONNECT {target}:{port} HTTP/1.1 (Proxy {proxy.label})\n" + meta.request
        try:
            connection.request("GET", checked.path, headers=headers)
            meta.tls_issuer = _issuer(connection.sock)
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
        hint = " – bei einem TLS-aufbrechenden Firmenproxy dessen Stammzertifikat in web.proxy.caFile angeben" if proxy else ""
        meta.error = f"TLS-Fehler (Zertifikat/Hostname): {err.reason or err}{hint}"
    except (OSError, http.client.HTTPException) as err:
        text = str(err)
        if proxy is not None and "Tunnel connection failed" in text:
            text = f"Proxy {proxy.label} hat den Tunnel abgelehnt ({text.split(':', 1)[-1].strip()})"
            if "407" in text:
                text += " – Anmeldung am Proxy nötig (web.proxy.username/passwordFile; NTLM/Kerberos nur über einen lokalen Helfer)"
        meta.error = f"Verbindung fehlgeschlagen: {text}"
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
