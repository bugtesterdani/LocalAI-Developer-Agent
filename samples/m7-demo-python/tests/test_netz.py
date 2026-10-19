"""Demo (Spezifikation §16): Testcode versucht absichtlich, das Netz zu erreichen – Internet, LAN und den Ollama-Port.
In der Offline-VM (ohne Netzwerkkarte) muss jeder Versuch scheitern; dann ist dieser Test grün."""

import socket

import pytest

ZIELE = [("example.com", 443), ("1.1.1.1", 443), ("10.0.0.1", 11434), ("192.168.178.1", 11434)]


@pytest.mark.parametrize("host,port", ZIELE)
def test_testcode_erreicht_kein_netz(host, port):
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=3).close()
