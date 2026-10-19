"""Web.Fetch (B09): Abruf genau einer genehmigten HTTPS-URL und Content Gate (Spezifikation §7).

Ablauf (jeweils eigener Prozess, unter Windows mit eingeschränktem Token auf niedriger Integritätsstufe):

1. ``fetch`` – prüft URL und alle DNS-Antworten (nur öffentliche Adressen), verbindet sich mit genau der geprüften
   Adresse, TLS mit Hostnamenprüfung, HTTPS GET ohne Cookies/Proxy, Weiterleitungen werden nicht verfolgt.
   Rohbytes landen in der Quarantäne (``raw.bin`` + ``meta.json``).
2. ``gate`` – liest nur die Quarantäne, extrahiert inerten Text (keine Skripte, Styles, versteckten Elemente,
   aktiven Links), markiert Bidi-/unsichtbare Zeichen und verdächtige Anweisungen (``text.txt`` + ``gate.json``).

Der Broker gibt den Text erst nach einer zweiten, eigenen Freigabe an einen Agenten weiter (``Web.Content``).
"""

SANITIZER_VERSION = "1"
