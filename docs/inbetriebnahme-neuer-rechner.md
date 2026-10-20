# Inbetriebnahme auf einem neuen Rechner

Stand: M5 (Commit `ec1c2bf`, 19.10.2026). Diese Anleitung bringt den aktuellen Stand des Local AI Developer Agent
auf einen weiteren Windows-PC: Oberfläche, Broker, Agenten, Offline-VM und Toolchains.

> Ergänzungen seit M5: Webabrufe (M6, Abschnitt 5), Linux-Gast unter Hyper-V ([pruefung-M8.md](pruefung-M8.md)),
> OS-Debugging ([pruefung-M9.md](pruefung-M9.md)). Für einen **Linux-Host** gilt stattdessen
> [pruefung-M10.md, Abschnitt 0](pruefung-M10.md#0-einrichtung). Alle Einstellungen:
> [Konfiguration](konfiguration.md).

**Kurz gesagt:** Das Repository und deine Dateien (Konfiguration, Toolchain-Ordner, Windows-ISO) mitnehmen. Alles,
was an die Hardware oder an den alten PC gebunden ist (VM, Kalibrierung, Registrierungen), **auf dem neuen Rechner neu
erzeugen**.

---

## 1. Was mitnehmen – und was nicht

| Was | Wo (alter PC) | Mitnehmen? | Warum |
|---|---|---|---|
| Repository (Code, Doku, Git-Verlauf) | `C:\Users\Daniel\Desktop\app6` | **ja** | – |
| Deine Konfiguration | `config\localai.json`, `config\demo-project.json`, `config\models.local.json` | **ja**, danach Pfade anpassen | gehören dir, sind nicht im Git (bzw. lokal geändert) |
| `config.zip` | im Repository-Ordner | ja, unverändert | deine Datei |
| Toolchain-Ordner | bei dir `C:\Users\Daniel\Desktop\app6_custom\Toolchains` | **ja** (oder neu anlegen, siehe 6) | enthält Installer, ZIPs, Offline-Pakete; groß |
| Windows-ISO | bei dir `…\app6_custom\image\…iso` | **ja** | Grundlage der VM |
| HALCON-Lizenz (falls genutzt) | im Toolchain-Ordner `hdevelop\license` | nur wenn sie nicht an den alten PC gebunden ist | VM hat weder Netzwerkkarte noch USB |
| Datenordner des Brokers | `%LOCALAPPDATA%\LocalAi\broker` | **nein** (Ausnahme: siehe unten) | Kalibrierung gilt nur für diese Hardware, Registrierungen enthalten feste Pfade |
| VM, Transfer-Laufwerke | `%LOCALAPPDATA%\LocalAi\vm` | **nein** | wird auf dem neuen PC gebaut (nie kopieren) |
| Abnahme-Ergebnisse | `%LOCALAPPDATA%\LocalAi\abnahme`, `broker-m4`, `broker-m5` | nein (nur zur Erinnerung) | – |
| Audit-Journal | `%LOCALAPPDATA%\LocalAi\broker\audit.db` | optional, als Archiv | auf dem neuen PC beginnt ein neues Journal |

Ausnahme Datenordner: `orchestrator.log` und `kalibrierung-*.md` kannst du als Vergleich mitnehmen; benutzt werden
sie auf dem neuen PC nicht.

## 2. Voraussetzungen auf dem neuen Rechner

- **Windows 11 Pro** (oder Enterprise/Education) – Hyper-V gibt es nicht in Home.
- Virtualisierung im BIOS/UEFI an (Intel VT-x/AMD-V), 16 GB RAM oder mehr empfohlen (die VM nimmt `memoryMB`,
  bei dir 4096), **mindestens 30 GB frei** auf dem Laufwerk des VM-Ordners, mit C++-Toolchain eher 80 GB.
- Administratorrechte (einmalig für Hyper-V; beim VM-Aufbau fragt die UAC).
- Internet für die Einrichtung (Pakete, Modelle, Toolchain-Dateien). Die VM selbst bekommt nie Netzwerk.

Installieren:

| Programm | Hinweis |
|---|---|
| **.NET 10 SDK** | `dotnet --version` → 10.x |
| **uv** | z. B. `winget install astral-sh.uv`; holt Python 3.15 selbst (`src/python/.python-version`) |
| **Git** | für Verlauf und spätere Updates |
| **Ollama** | nur wenn die Modelle auf diesem Rechner laufen sollen (sonst: anderer Rechner im LAN, siehe 5) |

Bei Proxy im Netz: dieselben Proxy-Variablen wie auf dem alten PC setzen (`HTTPS_PROXY`, `HTTP_PROXY`), bevor du
`uv sync` oder `dotnet restore` ausführst.

## 3. Repository übertragen

**Variante A – ganzer Ordner (am einfachsten):** `app6` komplett kopieren (USB-Platte, Netzlaufwerk), z. B. nach
`C:\Users\<Name>\Desktop\app6`. Vorher auf dem alten PC Oberfläche schließen.
Danach auf dem neuen PC die Build-Ordner entfernen, damit nichts Altes mitläuft:

```powershell
cd C:\Users\<Name>\Desktop\app6
Get-ChildItem -Recurse -Directory -Include bin,obj,.venv,__pycache__ | Remove-Item -Recurse -Force
```

**Variante B – nur Git-Verlauf plus deine Dateien:** auf dem alten PC

```powershell
cd C:\Users\Daniel\Desktop\app6
git bundle create D:\Transfer\localai.bundle --all
```

auf dem neuen PC

```powershell
git clone D:\Transfer\localai.bundle C:\Users\<Name>\Desktop\app6
```

und dann **von Hand** aus dem alten Ordner nachkopieren: `config\localai.json`, `config\demo-project.json`,
`config\models.local.json`, `config.zip`.

Prüfen: `git log --oneline -1` zeigt `ec1c2bf M5: Alle Sprachen …` (oder neuer).

## 4. Bauen und testen (ohne VM, ohne Modell)

```powershell
cd C:\Users\<Name>\Desktop\app6
uv sync --project src/python
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

Erwartet: „Alles grün.“ (C# 108 Tests, Python 253; einige Windows-/Abnahmetests werden übersprungen).
Schlägt hier etwas fehl, zuerst `dotnet --version`, `uv --version` und die Proxy-Variablen prüfen.

## 5. Konfiguration anpassen

### `config\localai.json`

Die Pfade auf den neuen Rechner umstellen:

```json
{
  "project": "config/m4-csharp-project.json",
  "toolchains": { "directory": "D:\\Agent\\Toolchains" },
  "sandbox": {
    "mode": "hyperv",
    "hyperv": {
      "imagePath": "D:\\Agent\\WindowsImage\\Windows11.iso",
      "imageName": "Windows 11 Pro",
      "memoryMB": 4096,
      "processors": 4,
      "keepVmOnExit": true,
      "reuseVm": true
    }
  }
}
```

- `toolchains.directory` und `sandbox.hyperv.imagePath`: neue Orte (absolute Pfade, `\\` in JSON).
- `memoryMB`/`processors` an den neuen PC anpassen (mehr RAM → C++-Builds deutlich schneller, z. B. 8192).
- Die übrigen Werte (Zeitgrenzen, `pauseOnNoAnswer`, …) kannst du übernehmen; siehe `docs/pruefung-M4.md`.
  `buildTimeoutMinutes` wird bei großen Toolchains automatisch erhöht.
- Zum ersten Ausprobieren ohne VM geht auch `"mode": "dev-local-unsafe"` (rot markiert, kein Build/Test).
- Webabrufe (ab M6): ohne Abschnitt `web` eingeschaltet mit Standardwerten. Ausschalten mit
  `"web": { "enabled": false }`. Hinter einem Firmenproxy: `web.proxy.url` (und ggf. `web.caFile` für einen
  TLS-aufbrechenden Proxy) setzen – die Proxy-Variablen (`HTTPS_PROXY`) gelten für Webabrufe bewusst **nicht**.
  Details: `docs/pruefung-M6.md`, Abschnitt 0.

### `config\models.local.json` (Ollama)

- Ollama auf **diesem** Rechner: Datei weglassen oder nur `limits` darin – es gilt `http://127.0.0.1:11434`.
- Ollama auf **einem anderen** Rechner im LAN (wie bisher 192.168.178.39):
  ```json
  { "ollama": { "url": "http://192.168.178.39:11434", "allowLan": true } }
  ```
  Auf dem Ollama-Rechner die Firewall-Freigabe für TCP 11434 um die IP des **neuen** PCs ergänzen.
- Deine `limits` (Schritte je Rolle, Ablehnungen …) bleiben gültig.

### Projektdateien

`config\demo-project.json` und eigene Projektdateien enthalten Ordnerpfade (`grants[].root`): auf den neuen Rechner
anpassen. Die Beispielprojekte `config\m4-*`/`m5-*` verweisen relativ auf `samples\` und passen ohne Änderung.

## 6. Ollama und Modelle (wenn lokal)

1. Ollama installieren, Umgebungsvariablen setzen (Benutzervariablen), Ollama neu starten:

   | Variable | Wert |
   |---|---|
   | `OLLAMA_HOST` | `127.0.0.1:11434` (bzw. `0.0.0.0:11434`, wenn andere Rechner zugreifen sollen) |
   | `OLLAMA_MAX_LOADED_MODELS` | `1` |
   | `OLLAMA_NUM_PARALLEL` | `1` |
   | `OLLAMA_FLASH_ATTENTION` | `1` |
   | `OLLAMA_KV_CACHE_TYPE` | `q8_0` |
   | `OLLAMA_NO_CLOUD` | `1` |

2. Modelle ziehen (feste Tags):
   ```powershell
   ollama pull qwen2.5-coder:7b
   ollama pull qwen2.5-coder:14b
   ```
   optional `qwen3:8b`, `qwen3-coder:30b`.

## 7. Kalibrierung (immer neu)

Die Kalibrierung gehört zur Hardware (GPU, Speicher) und zum Modell-Digest – auf dem neuen PC **neu** durchführen,
sonst verwendet der Orchestrator kein Profil:

```powershell
uv run --project src/python python -m localai.orchestrator.calibrate --profile leicht
uv run --project src/python python -m localai.orchestrator.calibrate --profile standard
```

Ollama auf einem anderen Rechner: zusätzlich `--ohne-vram-messung` (die GPU dort ist von hier nicht messbar).
Ergebnis: `%LOCALAPPDATA%\LocalAi\broker\calibration.json` und ein Bericht `kalibrierung-*.md`. Details:
`docs/pruefung-M3.md`, Abschnitt 3.

## 8. Hyper-V einrichten (einmalig)

PowerShell **als Administrator** im Repository:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sandbox\Enable-LocalAiHyperV.ps1
```

1. Beim ersten Mal aktiviert das Skript Hyper-V → **Neustart** → Skript **erneut** ausführen.
2. Es nimmt dich in die Gruppe „Hyper-V-Administratoren“ auf → **abmelden und wieder anmelden**.

Prüfen: `Get-VM` läuft ohne Fehler (auch ohne Administrator-PowerShell).

## 9. Toolchains

**Wenn du den Toolchain-Ordner kopiert hast:** an den neuen Ort legen (Pfad wie in `toolchains.directory`). Die
Registrierungen vom alten PC **nicht** übernehmen – sie enthalten die alten Pfade. Stattdessen neu registrieren
(Schritt 10).

Wichtig bei der dotnet-Toolchain: deine angepasste `toolchain.json` (ohne `--no-restore`, mit
`-p:RestoreSources={packages}`) mitnehmen; die Sicherung `dotnet-toolchain.json.vor-m4fix3` brauchst du nicht.

**Wenn du neu anlegst:**

| Toolchain | Vorgehen |
|---|---|
| `dotnet` | Vorlage `toolchains\dotnet` + SDK-ZIP, dann `scripts\toolchains\Get-NuGetOfflinePackages.ps1 -ToolchainDirectory <Ordner>\dotnet` (`toolchains\README.md`) |
| `python` | Vorlage `toolchains\python` + Installer + pytest-Wheels in `wheels\` |
| `cpp`, `powershell`, `batch`, `hdevelop` | `powershell -ExecutionPolicy Bypass -File scripts\toolchains\New-M5Toolchains.ps1 -Target <Toolchain-Ordner>` (`docs/pruefung-M5.md`, Abschnitt 0) |

HALCON: Ist die Lizenz an den alten PC gebunden (MAC/Dongle), gilt sie weder auf dem neuen PC noch in der VM. Dann
`hdevelop` aus den Projekten nehmen oder eine passende Lizenz besorgen.

## 10. Erster Start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

1. Bereich **TOOLCHAINS** → „Toolchain-Ordner prüfen“ → jede Toolchain, die das Projekt nutzt, **registrieren**
   (proprietäre: Lizenz bestätigen).
2. Die VM wird aufgebaut (UAC bestätigen). Dauer: ohne C++ ca. 15–30 Minuten, mit C++-Build-Tools 60–90 Minuten.
   Fortschritt in der Kopfzeile; danach z. B. „Offline-VM bereit … Toolchains: dotnet … (Hallo-Welt ok) 40 s, …“.
3. Mit `reuseVm: true` wird die VM beim Schließen gespeichert und beim nächsten Start fortgesetzt; Code-Änderungen
   (Worker, Gastskripte) spielt ein Gast-Update ein, ohne Neuaufbau.

## 11. Funktion prüfen

In dieser Reihenfolge, jeweils nach der Anleitung im Repository:

1. **Ohne Modell, mit VM:** M4-Abnahme-Skript (C#/Python, SEC-13/-20, großes Repo)
   ```powershell
   $env:LOCALAI_M4_ABNAHME = "1"
   uv run --project src/python pytest -s tests/integration/test_m4_abnahme.py
   ```
   Erwartet: `"ok": true` in `%LOCALAPPDATA%\LocalAi\abnahme\m4-abnahme.json`.
2. **Mit Modell:** in der Oberfläche `docs/pruefung-M4.md` Abschnitt 3 (C#-Rechner „Teilen … Mit Tests“).
3. **Weitere Sprachen:** `docs/pruefung-M5.md` (Abnahme-Skript `test_m5_abnahme.py`, danach je Sprache mit Modell).

## 12. Checkliste

- [ ] Windows 11 Pro, Virtualisierung im BIOS an, genug Platz
- [ ] .NET 10 SDK, uv, Git (ggf. Ollama) installiert, Proxy-Variablen gesetzt
- [ ] Repository übertragen, `git log` zeigt den aktuellen Stand
- [ ] `config\localai.json`, `config\demo-project.json`, `config\models.local.json`, `config.zip` übernommen,
      Pfade angepasst
- [ ] `scripts\test.ps1` → „Alles grün“
- [ ] Ollama erreichbar (`curl.exe http://127.0.0.1:11434/api/version` bzw. LAN-Adresse), Modelle vorhanden
- [ ] Kalibrierung neu, Profil freigegeben
- [ ] Hyper-V aktiviert, Gruppe „Hyper-V-Administratoren“, neu angemeldet
- [ ] Toolchain-Ordner und ISO am neuen Ort, Toolchains in der Oberfläche **neu registriert**
- [ ] VM aufgebaut, Kopfzeile „Offline-VM bereit“, Installationszeiten notiert
- [ ] M4-Abnahme-Skript grün, danach Ende-zu-Ende mit Modell

## 13. Typische Stolpersteine

| Problem | Abhilfe |
|---|---|
| „Hash weicht ab“ / „Ordner der registrierten Toolchain nicht gefunden“ | Registrierungen stammen vom alten PC oder der Ordner wurde verändert → TOOLCHAINS prüfen und neu registrieren |
| „Profil nicht kalibriert“ im Chat | Schritt 7 – Kalibrierung gilt je Hardware und Modell-Digest |
| VM-Aufbau „gesperrt: Administratorrechte … abgelehnt“ | UAC bestätigen; Gruppe „Hyper-V-Administratoren“ und neu anmelden (Schritt 8) |
| „Edition nicht im Abbild gefunden“ | `imageName` passt nicht zum ISO; die Meldung nennt die vorhandenen Editionen |
| Ollama im LAN nicht erreichbar | Firewall auf dem Ollama-Rechner für die IP des neuen PCs öffnen, `OLLAMA_HOST=0.0.0.0:11434` |
| `uv sync` / `dotnet restore` hängt | Proxy-Variablen fehlen |
| VM wird bei jedem Start neu aufgebaut | `reuseVm` nicht `true`, Projekt wählt andere Toolchains als beim letzten Aufbau, oder die letzte Sitzung endete nicht sauber (dann einmal neu) |
