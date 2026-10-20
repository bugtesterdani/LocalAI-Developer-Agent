# Lizenzen und Drittanbieter

Der eigene Quellcode und die eigene Dokumentation stehen unter der MIT-Lizenz
in [LICENSE](LICENSE). Fremdkomponenten behalten ihre jeweiligen Lizenzen.
Die MIT-Lizenz des Projekts erteilt keine Rechte an fremden Programmen,
Modellgewichten, Betriebssystemabbildern oder persönlichen Lizenzdateien.

## Mitgelieferte Laufzeitkomponenten

Die tatsächlichen Versionen und vollständigen Original-Lizenztexte werden beim
Paketieren aus den installierten Python-Distributionen, den aufgelösten
NuGet-Paketen und der mitgelieferten Python-Laufzeit gesammelt.
Im Installer liegen sie unter `licenses/collected/`; dazu gehören
`THIRD-PARTY-INVENTORY.json` und `THIRD-PARTY-INVENTORY.md`.
Bei NuGet wird der veröffentlichte Laufzeitgraph aus deps.json ausgewertet;
reine Buildwerkzeuge und Analyzer werden nicht als mitgelieferte Komponenten
aufgeführt. Die Inventarliste ist keine juristische Freigabe.

| Bestandteil | Lizenz / Hinweis |
|---|---|
| .NET / WPF und Microsoft.Data.Sqlite | MIT für die genannten Projekte; native und transitive Komponenten separat beachten |
| Avalonia | MIT für das Framework; native Abhängigkeiten separat beachten |
| Inter (Avalonia.Fonts.Inter) | SIL Open Font License 1.1; Originaltext in licenses/Inter-OFL-1.1.txt |
| Python | PSF-2.0 und weitere enthaltene Lizenztexte |
| Pydantic / pydantic-core | MIT; transitive Python-Pakete separat inventarisiert |
| Gebündelte Python-Distribution | python-build-standalone enthält zusätzliche native Bibliotheken; deren mitgelieferte Hinweise bleiben erhalten |

Die Inter-Schrift bleibt unter OFL; die OFL verlangt nicht, den Anwendungscode
ebenfalls unter OFL zu veröffentlichen. Die Originaltexte in den konkreten
Paketen haben Vorrang vor dieser Übersicht. Tests und Buildwerkzeuge haben
eigene Bedingungen und werden nicht pauschal durch die Projektlizenz erfasst.

## Extern installierte Software

QEMU, bubblewrap, mtools, dosfstools und Debian-Pakete werden über das
Betriebssystem bzw. separate Downloads bezogen. Diese Projektlizenz lizenziert
sie nicht neu. Bei einer eigenen Weitergabe von Binaries, Gastabbildern oder
Offline-Bundles sind deren Lizenztexte, Copyright-Hinweise und gegebenenfalls
die passenden Quelltexte bzw. ein zulässiges Quelltextangebot erforderlich.
Der Start eines separaten QEMU-Prozesses allein stellt den Agentencode nicht
automatisch unter GPL. QEMU selbst steht als Ganzes unter GPLv2 mit
komponentenspezifischen Lizenzen.

Windows, Visual Studio Build Tools, Windows SDK, VC++ Redistributable und
MVTec HALCON sind separat lizenziert. Download, Installation in einer VM und
Weitergabe müssen jeweils von den tatsächlichen Nutzungsrechten gedeckt sein.
Eine Bestätigung in der Oberfläche ist kein Erwerb dieser Rechte.
Die Toolchain-Vorlagen enthalten eigene Konfigurationen und Runner; proprietäre
Installationsdateien und persönliche HALCON-Lizenzen gehören nicht in Releases.

## Modelle und erzeugter Code

Ollama und Modellgewichte werden separat installiert. Die konfigurierten
Modellnamen sind keine Lizenznachweise. Vor der Weitergabe von Gewichten:
den exakten Digest, Herkunft, Lizenz und etwaige NOTICE-Dateien dokumentieren.
Das gilt auch für quantisierte Fassungen und geänderte Modell-Tags.
Modellgenerierter Code kann fremden Code enthalten und muss vor einer
Veröffentlichung auf Herkunft und erforderliche Hinweise geprüft werden.

## Quellen

- https://github.com/dotnet/runtime/blob/main/LICENSE.TXT
- https://github.com/dotnet/efcore/blob/main/LICENSE.txt
- https://github.com/AvaloniaUI/Avalonia/blob/master/licence.md
- https://github.com/rsms/inter/blob/master/LICENSE.txt
- https://docs.python.org/3/license.html
- https://github.com/pydantic/pydantic/blob/main/LICENSE
- https://github.com/pydantic/pydantic-core/blob/main/LICENSE
- https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html
- https://www.qemu.org/docs/master/about/license.html
- https://visualstudio.microsoft.com/license-terms/
- https://www.mvtec.com/products/halcon

Aktuelle Upstream-Seiten dienen der Orientierung. Für eine konkrete
Auslieferung sind die versionsgenauen, mitgelieferten Texte maßgeblich.
