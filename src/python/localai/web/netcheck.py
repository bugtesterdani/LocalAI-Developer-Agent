"""URL- und Adressregeln für Web.Fetch (SSRF-Abwehr, SEC-11). Gleiche Regeln prüft der Broker (UrlPolicy.cs).

* Nur ``https``, Port 443, keine Zugangsdaten in der URL, höchstens 2.048 Zeichen, keine Steuer-/Leerzeichen.
* Hostname: IDNA, mindestens ein Punkt, keine internen Namensräume (``localhost``, ``.local``, ``.internal`` …).
  IP-Literale nur in kanonischer Schreibweise und nur öffentlich; Zahlenformen wie ``2130706433``, ``0x7f.1`` oder
  ``127.1`` werden abgelehnt (alternative IP-Schreibweisen).
* Jede Adresse aus DNS muss öffentlich sein (sonst Abbruch – auch wenn nur eine Antwort privat ist); verbunden wird
  mit genau der geprüften Adresse (DNS-Rebinding).
* Gesperrt: private, Loopback-, Link-local-, Multicast-, reservierte, unspezifizierte, CGNAT- und Dokumentations-
  adressen, Metadatenadressen; IPv4 in IPv6 (mapped, 6to4, Teredo, NAT64) wird auf die eingebettete Adresse geprüft.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL = 2048
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".home.arpa", ".corp",
                     ".localdomain", ".arpa", ".onion", ".test", ".invalid", ".example")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+))*\.?$", re.I)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_EXTRA_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32", "64:ff9b:1::/48", "2001:db8::/32",
    "fc00::/7", "fe80::/10", "ff00::/8", "::/128", "::1/128",
)]


class UrlRejected(ValueError):
    """URL oder Zieladresse ist nicht erlaubt (deutsche Meldung für Nutzer und Agent)."""


@dataclass(frozen=True)
class CheckedUrl:
    url: str  # ohne Fragment
    host: str  # IDNA (ASCII), klein
    path: str  # Pfad + Query für die Anfragezeile
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public(address.ipv4_mapped)
        if address.sixtofour is not None and not is_public(address.sixtofour):
            return False
        if address.teredo is not None and not all(is_public(a) for a in address.teredo):
            return False
        if address in _NAT64:
            return is_public(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    if any(address in net for net in _EXTRA_BLOCKED if net.version == address.version):
        return False
    return bool(address.is_global) and not (address.is_private or address.is_loopback or address.is_link_local
                                            or address.is_multicast or address.is_reserved or address.is_unspecified)


def check_url(url: str) -> CheckedUrl:
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise UrlRejected(f"URL fehlt oder ist länger als {MAX_URL} Zeichen")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise UrlRejected("URL enthält Leer- oder Steuerzeichen")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UrlRejected("nur https:// ist erlaubt")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRejected("Zugangsdaten in der URL sind nicht erlaubt")
    try:
        port = parts.port
    except ValueError as err:
        raise UrlRejected("ungültiger Port") from err
    if port not in (None, 443):
        raise UrlRejected("nur Port 443 ist erlaubt")
    raw_host = parts.hostname or ""
    if not raw_host:
        raise UrlRejected("Hostname fehlt")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if parts.netloc.startswith("["):
        try:
            literal = ipaddress.IPv6Address(raw_host)
        except ValueError as err:
            raise UrlRejected("ungültige IPv6-Adresse") from err
        host = raw_host
    else:
        try:
            literal = ipaddress.IPv4Address(raw_host)
        except ValueError:
            literal = None
        if literal is None and _NUMERIC_HOST.match(raw_host):
            raise UrlRejected("IP-Adresse in ungewöhnlicher Schreibweise – nur kanonisch (z. B. 93.184.215.14)")
        if literal is not None:
            host = str(literal)
        else:
            try:
                host = raw_host.encode("idna").decode("ascii").lower()
            except UnicodeError as err:
                raise UrlRejected("Hostname ist kein gültiger Domainname") from err
            if "." not in host.strip(".") or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
                raise UrlRejected(f"Hostname {host} ist kein öffentlicher Domainname")
            if not re.fullmatch(r"[a-z0-9.-]+", host) or any(len(label) > 63 or not label for label in host.rstrip(".").split(".")):
                raise UrlRejected(f"Hostname {host} ist ungültig")
    if literal is not None and not is_public(literal):
        raise UrlRejected(f"Adresse {literal} ist nicht öffentlich (privat, lokal oder reserviert)")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    clean = f"https://{('[' + host + ']') if isinstance(literal, ipaddress.IPv6Address) else host}{path}"
    return CheckedUrl(clean, host, path, literal)


def check_addresses(host: str, addresses: list[str]) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Alle DNS-Antworten müssen öffentlich sein; liefert die Adresse, mit der verbunden wird."""
    if not addresses:
        raise UrlRejected(f"{host}: keine Adresse gefunden")
    parsed = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(raw.split("%", 1)[0]))
        except ValueError as err:
            raise UrlRejected(f"{host}: ungültige DNS-Antwort {raw!r}") from err
    blocked = [str(a) for a in parsed if not is_public(a)]
    if blocked:
        raise UrlRejected(f"{host} löst (auch) auf nicht öffentliche Adressen auf: {', '.join(blocked[:4])} – abgebrochen")
    ipv4 = [a for a in parsed if a.version == 4]
    return (ipv4 or parsed)[0]
