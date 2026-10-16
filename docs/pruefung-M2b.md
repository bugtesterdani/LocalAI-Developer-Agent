# Prüfanleitung M2b – Offline-VM mit Hyper-V

Ziel: bestätigen, dass die Sandbox-VM sich bei Sitzungsstart unbeaufsichtigt neu installiert, **nie eine Netzwerkkarte hat**, den Selbsttest besteht und Jobs nur über Copy-VMFile (hin) und die Transfer-VHDX (zurück) laufen. Gemessen werden die Aufbauzeit und 50 Roundtrips.

Toolchains (.NET, Python für Agentenprojekte) folgen in M2c. In M2b läuft in der VM nur der Worker mit File.List/Read/ReadMany/Search.

## Wie es funktioniert (kurz)

| Schritt | Wer | Rechte |
|---|---|---|
| VM-Neuaufbau: VHDX anlegen, Windows per DISM anwenden, `unattend.xml`, Gastdateien, VM **ohne NIC** erstellen und starten | `scripts\sandbox\New-LocalAiSandbox.ps1` | Administrator (UAC-Abfrage bei jedem Sitzungsstart) |
| Erster Start im Gast: Windows richtet sich ein, `SetupComplete.cmd` → `provision.ps1`: Netzwerkprüfungen, Worker als geplante Aufgabe (SYSTEM), Bericht | Gast | – |
| Job hin: signierter Job + Nutzlast + `.ready` per `Copy-VMFile` nach `C:\LocalAi\inbox` | `Send-LocalAiJob.ps1` | Hyper-V-Administratoren |
| Fertig-Signal: Gast setzt KVP-Wert `LocalAiSignal = result:<Sequenz>` | `Watch-LocalAiSignal.ps1` | Hyper-V-Administratoren |
| Ergebnis zurück: Transfer-VHDX abhängen, Broker liest sie **ohne Einbinden** (eigener VHDX-Leser, kein Dateisystem), frische leere VHDX anhängen | `Switch-LocalAiTransferDisk.ps1` + Broker | Hyper-V-Administratoren |
| Sitzungsende: VM und Festplatten löschen | `Remove-LocalAiSandbox.ps1` | Hyper-V-Administratoren |

## 0. Einmalige Einrichtung (PC 123)

1. **PowerShell als Administrator** im Projektordner:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sandbox\Enable-LocalAiHyperV.ps1
   ```
   Aktiviert bei Bedarf Hyper-V (dann Neustart und Skript erneut) und nimmt dich in die Gruppe »Hyper-V-Administratoren« auf. **Danach ab- und wieder anmelden.**
2. **Windows-ISO** ablegen, z. B. `D:\Agent\WindowsImage\windows11.iso` (Windows 11 Pro mit deiner Lizenz oder die Enterprise-Evaluierung).
3. `config\localai.json` → `sandbox.hyperv` anpassen:
   - `imagePath`: Pfad zur ISO (oder direkt zu einer `install.wim`/`install.esd`).
   - `imageName`: Edition, z. B. `"Windows 11 Pro"` bzw. `"Windows 11 Enterprise Evaluation"`. Stimmt der Name nicht, nennt die Fehlermeldung alle Editionen im Abbild. Alternativ `imageIndex` (dann `imageName` auf `null`).
   - `memoryMB` (Standard 8192) und `processors` (4) bei Bedarf an den PC anpassen.
   - `vmDirectory`: `null` = `%LOCALAPPDATA%\LocalAi\vm`. Ein eigener Ordner muss leer sein. Platzbedarf etwa 20–30 GB.
4. `uv sync --project src/python` (wie gewohnt mit Proxy-Variablen). Aus dieser Umgebung stammen Python und pydantic für den Gast – in der VM wird nichts heruntergeladen.

## 1. Automatische Tests (ohne VM)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

| Schritt | Erwartet |
|---|---|
| 3/4 C#-Tests | `Passed: 54` – darunter `HyperVTests` (simulierter Host mit echtem Worker im VM-Modus), `TransferTests` (VHDX ohne Einbinden lesen, auch eine von qemu erzeugte), `PowerShell_Skripte_sind_gueltig` (Syntax aller Skripte unter Windows PowerShell 5.1) |
| 4/4 Python-Tests | `104 passed, 2 skipped` (übersprungen: die Hyper-V-Abnahme aus Abschnitt 2 und ein Test, der nur ohne Windows sinnvoll ist) |

## 2. Abnahme mit echter VM (ohne Oberfläche, 15–60 Minuten)

```powershell
$env:LOCALAI_HYPERV_ABNAHME = "1"
uv run --project src/python pytest -s tests/integration/test_hyperv_abnahme.py
```

- [ ] UAC-Abfrage erscheint (PowerShell) → **Ja**.
- [ ] Ausgabe zeigt nacheinander: »VM wird aufgebaut – Schritt 1/9 …« bis »Schritt 9/9: VM starten«, dann »Windows richtet sich in der VM ein …«, dann »Offline-VM bereit – Selbsttest bestanden (6 Netzwerkprüfungen ohne Verbindung, Python …)«.
- [ ] Im Hyper-V-Manager: VM `LocalAi-Sandbox`, unter Einstellungen **kein Netzwerkadapter**, zwei Festplatten am SCSI-Controller.
- [ ] Snapshot und `50/50 Roundtrips`, Test grün.
- [ ] Danach ist die VM aus dem Hyper-V-Manager verschwunden.
- [ ] Ergebnis mit Zeiten: `%LOCALAPPDATA%\LocalAi\abnahme\hyperv-abnahme.json` – **bitte an Claude schicken** (Aufbauzeit und Roundtrip-Dauer werden dokumentiert).

Bei Fehlern bitte schicken:
- Konsolenausgabe
- `%LOCALAPPDATA%\LocalAi\abnahme\vm\build.log` (und ggf. `dism.log`)
- `%LOCALAPPDATA%\LocalAi\abnahme\broker\hyperv.log`

Hängt es bei »Windows richtet sich in der VM ein«: im Hyper-V-Manager »Verbinden« und den Bildschirm der VM ansehen (z. B. eine OOBE-Seite, die nicht übersprungen wurde) – Foto davon hilft. Zum Nachsehen im Gast `keepVmOnExit` auf `true` setzen: dann bleibt die VM nach dem Test stehen, das Kennwort für den Benutzer `localai` steht in `…\vm\gast-kennwort.txt`, die Gastprotokolle in `C:\LocalAi\logs`.

## 3. Oberfläche mit VM

In `config\localai.json` `"mode": "hyperv"` setzen, dann `scripts\start.ps1`.

- [ ] UAC-Abfrage → Ja. Kopfzeile (gelb): »SANDBOX: Offline-VM – VM wird aufgebaut – Schritt …«; Fortschritt auch unter »Ereignisse«.
- [ ] Die Oberfläche bleibt währenddessen bedienbar.
- [ ] Danach: »SANDBOX: Offline-VM – Offline-VM bereit – Selbsttest bestanden …; Aufbau N min«.
- [ ] Testlauf mit Fake-Agent wie in M2a: Snapshot, Auflisten, Lesen mit Abwahl – gleiche Ergebnisse, jetzt aus der VM. Ein Aufruf dauert ein paar Sekunden.
- [ ] Wird ein Vorschlag erlaubt, **bevor** die VM bereit ist: »Sandbox nicht bereit …«, nichts wird ausgeführt.
- [ ] Oberfläche schließen → VM ist nach kurzer Zeit aus dem Hyper-V-Manager verschwunden.

## 4. Gegenproben (empfohlen)

1. **Netzwerkkarte nachträglich (SEC-24):** während die VM bereit ist, in einer PowerShell
   `Add-VMNetworkAdapter -VMName LocalAi-Sandbox`
   → innerhalb weniger Sekunden Kopfzeile rot »gesperrt: … Netzwerkkarte …«; weitere Aufrufe scheitern. (Adapter wird mit der VM gelöscht.)
2. **UAC ablehnen:** Oberfläche starten, UAC mit »Nein« beantworten → »gesperrt: Administratorrechte für den VM-Aufbau wurden abgelehnt«.
3. **Falsche Edition:** `imageName` auf `"Gibt es nicht"` → »gesperrt: VM-Aufbau fehlgeschlagen: Edition nicht im Abbild gefunden. Vorhanden: …«.
4. **Fremder Ordner:** `vmDirectory` auf einen Ordner mit anderen Dateien → Aufbau verweigert, nichts wird gelöscht.

## Bekannte Einschränkungen

- `New-VM` legt immer einen (nicht verbundenen) Netzwerkadapter an. Das Skript entfernt ihn **vor dem ersten Start** und bricht ab, falls das nicht gelingt. Die VM läuft zu keinem Zeitpunkt mit Adapter; geprüft wird vor dem Start, bei jedem Job und alle paar Sekunden.
- Der Neuaufbau braucht bei jedem Sitzungsstart Administratorrechte (DISM, Einbinden der neuen Systemfestplatte).
- Keine Toolchains in der VM bis M2c.
