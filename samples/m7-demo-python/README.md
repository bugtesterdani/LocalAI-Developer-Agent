# M7-Demo (Python): Kontakte

Kleines Projekt für die Abnahme-Demo (Spezifikation §16). Ziel im Chat, z. B.:
„Füge einen CSV-Parser (RFC 4180) hinzu, der Kontakte einliest: Unicode, leere Felder (None), Fehler mit Zeilennummer. Mit Tests.“

`tests/test_netz.py` versucht absichtlich, das Netz zu erreichen (Internet, LAN, Ollama-Port) – in der Offline-VM muss
das scheitern, der Test ist dann grün. Auf einem PC mit Netz schlägt er fehl (gewollt).
