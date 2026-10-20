# Installation aus den Paketen

Fertige Pakete stehen bei jedem Release auf der GitHub-Seite des Projekts unter **Releases**. Sie enthalten alles,
was die Anwendung zur Laufzeit braucht: Oberfläche und Broker (.NET, eigenständig), Python mit den nötigen Paketen,
Skripte, Toolchain-Vorlagen und Beispielprojekte. .NET SDK und uv sind **nicht** nötig. Ollama installierst du
separat (siehe [Konfiguration](konfiguration.md#empfohlene-ollama-umgebung)).

| Datei | für | Hinweis |
|---|---|---|
| `LocalAi-Developer-Agent-<v>-x64.msi` | Windows 11 Pro | klassischer Installer nach Program Files, Startmenü-Eintrag |
| `LocalAi-Developer-Agent-<v>-x64.msixbundle` | Windows 11 Pro | App-Paket; bei selbstsigniertem Paket vorher das Zertifikat vertrauen (unten) |
| `LocalAi-Developer-Agent-<v>-signatur.cer` | MSIX | nur bei selbstsignierten Builds |
| `localai-developer-agent_<v>_amd64.deb` | Debian 12/13, Ubuntu 22.04/24.04 | installiert QEMU, bubblewrap usw. als Abhängigkeiten |
| `LocalAi-Konfiguration-<v>.zip` | alle | Vorlagen für die eigene Konfiguration, Toolchain-Vorlagen, empfohlene Ordnerstruktur |
| `LocalAi-Dokumentation-<v>.zip` | alle | diese Dokumentation als Markdown |

MSI und MSIX sind zwei Wege zum selben Ergebnis – nur einen davon installieren.

## Windows

### MSI

Doppelklick auf die `.msi`, Administratorrechte bestätigen. Installiert nach
`C:\Program Files\LocalAi Developer Agent`. Aktualisieren: neue `.msi` installieren; die Einstellungen im
Benutzerordner bleiben erhalten.

### MSIX

1. Nur wenn das Release eine `…-signatur.cer` enthält (selbstsigniert): Zertifikat einmalig vertrauen – PowerShell
   **als Administrator**:
   ```powershell
   Import-Certificate -FilePath .\LocalAi-Developer-Agent-<v>-signatur.cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople
   ```
2. Doppelklick auf die `.msixbundle` → **Installieren**, oder `Add-AppxPackage .\LocalAi-Developer-Agent-<v>-x64.msixbundle`.

Das Paket läuft mit voller Vertrauensstufe und ohne Dateisystem-Virtualisierung, damit Hyper-V die VM-Dateien unter
`%LOCALAPPDATA%\LocalAi` am echten Ort findet.

### Danach

1. Hyper-V einrichten (einmalig, PowerShell als Administrator):
   ```powershell
   powershell -ExecutionPolicy Bypass -File "C:\Program Files\LocalAi Developer Agent\scripts\sandbox\Enable-LocalAiHyperV.ps1"
   ```
   Bei MSIX liegt der Installationsordner unter `C:\Program Files\WindowsApps\…`:
   ```powershell
   & "$((Get-AppxPackage LocalAi.DeveloperAgent).InstallLocation)\scripts\sandbox\Enable-LocalAiHyperV.ps1"
   ```
   Weiter wie in der
   [Inbetriebnahme, Abschnitt 8](inbetriebnahme-neuer-rechner.md#8-hyper-v-einrichten-einmalig).
2. Eigene Konfiguration anlegen (siehe [unten](#eigene-konfiguration)).
3. Starten über das Startmenü: **Local AI Developer Agent**.

## Linux (Debian/Ubuntu)

```bash
sudo apt install ./localai-developer-agent_<v>_amd64.deb
sudo usermod -aG kvm "$USER"        # QEMU mit KVM; danach ab- und wieder anmelden
/opt/localai/scripts/linux/get-prerequisites.sh ~/LocalAi/linux   # Debian-Abbild und Bausteine für die VM
```

`apt` installiert die Abhängigkeiten (QEMU, qemu-utils, dosfstools, mtools, bubblewrap und die Bibliotheken der
Oberfläche) mit. Installiert wird nach `/opt/localai`, gestartet über das Anwendungsmenü oder mit `localai`.
Entfernen: `sudo apt remove localai-developer-agent` – die Einstellungen in `~/.config/localai` und die Daten in
`~/.local/share/LocalAi` bleiben.

## Eigene Konfiguration

Der Installationsordner ist schreibgeschützt. Eigene Einstellungen liegen deshalb im **Benutzerordner**:

| System | Ordner |
|---|---|
| Windows | `%LOCALAPPDATA%\LocalAi\config\` |
| Linux | `~/.config/localai/` (bzw. `$XDG_CONFIG_HOME/localai`) |

1. `LocalAi-Konfiguration-<v>.zip` entpacken.
2. `config/localai.windows.json` bzw. `config/localai.linux.json` als **`localai.json`** in den Benutzerordner
   kopieren und die Pfade anpassen (`imagePath`, `prerequisites`, `toolchains.directory`, `project`).
3. Optional `models.local.json` (Vorlage `config/models.local.example.json`) daneben legen, z. B. für Ollama im LAN.
4. Eigene Projektdatei mit **absoluten** Pfaden anlegen (Vorlage `config/demo-project.json`).

Regeln:

- Liegt `localai.json` im Benutzerordner, gilt sie; sonst die mitgelieferte Standardkonfiguration
  (`<Installationsordner>/config/localai.json`: Entwicklungsmodus `dev-local-unsafe` mit Demo-Projekt, damit der erste
  Start gelingt).
- Relative Pfade in der eigenen `localai.json` beziehen sich auf den **Installationsordner** – so funktionieren
  `config/models.json`, die Beispielprojekte und `runtime/python/…`. Eigene Dateien deshalb absolut angeben.
- `LOCALAI_CONFIG_DIR` setzt einen anderen Benutzerordner, `LOCALAI_CONFIG` eine bestimmte Datei.
- Alle Felder: [Konfiguration](konfiguration.md).

Die mitgelieferten Prozesse starten mit dem mitgelieferten Python, z. B.:

```json
"orchestrator": {
  "command": "runtime/python/python.exe",
  "arguments": ["-m", "localai.orchestrator"],
  "workingDirectory": "."
}
```

Unter Linux heißt das Programm `runtime/python/bin/python3`. Die Vorlagen im Konfigurations-ZIP sind bereits so
eingestellt.

## Pakete selbst bauen

Die Pakete entstehen in GitHub Actions (`.github/workflows/release.yml`):

- **Tag `v1.2.3` pushen** → alle Pakete bauen, testen und als Release veröffentlichen.
- **Actions → Release → Run workflow** → Pakete als Artefakte des Laufs, ohne Release.

Ohne Signaturzertifikat wird das MSIX-Paket selbstsigniert. Mit eigenem Zertifikat die Repository-Secrets
`MSIX_CERTIFICATE` (PFX als Base64) und `MSIX_CERTIFICATE_PASSWORD` setzen; der Betreff des Zertifikats wird als
Herausgeber ins Paket übernommen, und auch die MSI wird damit signiert.

Die Bausteine liegen unter `packaging/`: `package.py` (Installationsordner, ZIP-Archive, Symbole),
`windows/LocalAi.wxs` (MSI, WiX 5), `windows/AppxManifest.xml` (MSIX), `linux/` (Debian-Paket).
