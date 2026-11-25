# Edge0 als Modellserver

Der Orchestrator kann seine Modelle statt über Ollama (oder zusätzlich dazu) über **Edge0** beziehen. Edge0 ist ein
quelloffener Inferenz-Server (Apache 2.0, <https://github.com/Edge0-AI/Edge0>) für große Mixture-of-Experts-Modelle:
die Expertengewichte bleiben auf der SSD und werden nur bei Bedarf gelesen, dadurch braucht ein 35B-Modell nur wenige
GB Arbeitsspeicher. Der Server `edge0-infer serve` bietet eine OpenAI-kompatible Schnittstelle; LocalAI spricht davon
nur `/v1/chat/completions` (Streaming), `/v1/models` und `/health`.

Inhalt: [Einschätzung](#1-einschätzung-welches-modell-auf-welcher-hardware) ·
[Grundsatz „kein Internet“](#2-grundsatz-kein-internet) ·
[Phase A: beschaffen und bauen](#3-phase-a-beschaffen-und-bauen-mit-internet) ·
[Phase B: offline betreiben](#4-phase-b-offline-betreiben) ·
[Phase C: in LocalAI einrichten](#5-phase-c-in-localai-einrichten) · [Fehlerbilder](#6-fehlerbilder)

## 1. Einschätzung: welches Modell auf welcher Hardware

| | Edge0 35B (nativer Motor, `edge0_35b`) | Edge0 145B / 120B (über `edge0-strata`, `edge0_145b` / `edge0_120b`) |
|---|---|---|
| Rechenwerk unter Windows | CPU oder Vulkan (läuft mit dem normalen NVIDIA-/AMD-Treiber) | NVIDIA-GPU mit CUDA 13 |
| Speicher | wenige GB RAM; ~42 GB freier Platz für Download und Umwandlung | 145B Q3_K: 40,8 GiB Gewichte. Herstellertest mit 16 GiB VRAM + 20 GiB RAM; 12 GiB VRAM läuft laut Hersteller langsamer |
| Bauen | Visual Studio 2022 (C++), CMake, Ninja, uv, Git; für Vulkan das Vulkan SDK | zusätzlich CUDA-13-Toolkit und Python ≥ 3.10 |
| Reife | Vorschau. Die Windows-Motoren gelten laut Modellkarte als noch nicht ausgereift; Werkzeugnutzung, mehrstufige Planung und lange autonome Aufgaben nennt die Modellkarte als Schwächen | Modellkarte nennt Agenten und Coding als Einsatzgebiet |

Folgerungen für LocalAI:

- **Erst eine Rolle testen.** Die Agenten planen in mehreren Schritten – genau das nennt die 35B-Modellkarte als
  Schwäche. Am einfachsten ein Edge0-Profil nur für eine Rolle eintragen und die übrigen bei Ollama lassen
  (gemischter Betrieb, Abschnitt 5.2).
- **Ein Server = ein Modell.** `edge0-infer serve` lädt genau ein Modell. Unterschiedliche Modelle je Rolle gehen nur
  zusammen mit Ollama (mehrere Edge0-Server gleichzeitig sind in LocalAI derzeit nicht vorgesehen).
- **Eine Anfrage gleichzeitig.** Eine zweite Anfrage beantwortet Edge0 sofort mit HTTP 429; LocalAI wartet dann
  (`edge0.busyWaitSeconds`). Mit `limits.maxGpuRequests: 1` (Standard) kommt das im Betrieb nicht vor.
- **Kontext beim Serverstart.** Die Kontextlänge legt `--context` beim Start fest, nicht die Anfrage. `contextTokens`
  der Edge0-Profile darf nicht größer sein.
- **SSD:** Edge0 liest die Experten ständig von der SSD. Lesen verschleißt eine SSD praktisch nicht; geschrieben wird
  nur einmal beim Herunterladen/Umwandeln (bei 145B schreibt der erste Start zusätzlich eine Datei in den Modellordner).
  Ein schnelles NVMe-Laufwerk beschleunigt die Ausgabe spürbar.

## 2. Grundsatz „kein Internet“

Ziel: Nach der Einrichtung kann **kein** Teil von Edge0 eine Verbindung ins Internet aufbauen – unabhängig davon, was
die Programme selbst vorhaben. Dafür gibt es vier Schichten:

1. **Beschaffen und Betreiben trennen.** Alles, was aus dem Netz kommt (Quellcode, Build-Werkzeuge, Python-Pakete,
   Modell), wird in Phase A geholt. Im Betrieb (Phase B) laufen nur noch die fertigen Programme mit dem fertigen
   Modellordner. Das fertige Windows-Paket aus den GitHub-Releases kann man dafür nehmen (Abschnitt 3.0) – für LocalAI
   wird daraus nur `edge0-infer.exe` gebraucht. Die mitgelieferte Chat-Oberfläche wird nicht gestartet: sie kann Modelle
   von Hugging Face/ModelScope laden, und ihr Netzverhalten ist nicht dokumentiert. Die Sperre aus Schicht 2 erfasst sie
   trotzdem mit, weil sie alle Programme im Ordner betrifft.
2. **Windows-Firewall sperrt ausgehend.** `scripts\edge0\firewall.ps1` legt für **jedes** Programm (`*.exe`) im
   Edge0-Ordner eine Ausgangsregel „Blockieren“ an – auch für einen Python-Interpreter, den eine `.venv` dort benutzt.
   Eine Blockierregel gewinnt in der Windows-Firewall gegen normale Zulassungsregeln. Sie betrifft nur Verbindungen, die
   das Programm **selbst aufbaut**; Antworten auf eine erlaubte eingehende Verbindung (Orchestrator → Edge0) laufen
   weiter. Loopback (127.0.0.1) filtert Windows ohnehin nicht – Edge0 startet für 145B/120B intern einen zweiten
   Prozess und spricht ihn über Loopback an, das bleibt also möglich.
3. **Offline-Startumgebung.** `scripts\edge0\start.ps1` setzt vor dem Start `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`,
   `HF_HUB_DISABLE_TELEMETRY`, `UV_OFFLINE`, `DO_NOT_TRACK` und lenkt `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` auf eine tote
   lokale Adresse (`http://127.0.0.1:9`, Loopback ausgenommen). Würde eine Bibliothek doch etwas nachladen wollen,
   scheitert sie sofort lokal. Außerdem startet das Skript nur, wenn die Ausgangssperre aus Schicht 2 besteht.
4. **Nachprüfen** (Abschnitt 4.4): offene Verbindungen des Prozesses ansehen, optional verworfene Pakete protokollieren.

Was die Edge0-Dokumentation dazu sagt (nicht von uns überprüft – Schicht 2 und 3 gelten unabhängig davon): Der Build des
nativen Motors lädt nichts nach (GGML liegt fest im Repository); Netzzugriffe nennt sie nur fürs Herunterladen (Modell,
Python-Pakete, beim 145B-Build zusätzlich der llama.cpp-Quellcode). Der Server lauscht standardmäßig nur auf
`127.0.0.1:8000`, schreibt keine Gesprächsinhalte ins Protokoll, und weder Telemetrie noch eine Update-Prüfung werden
beschrieben.

Auf dem Rechner mit dem **Orchestrator** ist nichts weiter nötig: das Model Gateway verbindet sich nur mit Loopback
oder – ausdrücklich freigegeben – einer privaten LAN-IP (kein DNS-Name, keine öffentliche Adresse).

## 3. Phase A: beschaffen und bauen (mit Internet)

Phase A läuft am besten direkt auf dem Rechner, der Edge0 später betreibt, **im endgültigen Ordner** (Beispiel
`D:\Edge0`) – Python-Umgebungen (`.venv`) enthalten absolute Pfade und lassen sich nicht verschieben. Für 35B allein
genügt es auch, auf einem anderen Rechner zu bauen und nur `bin\` und den Modellordner zu kopieren.

### 3.0 Kürzester Weg: fertiges Release statt selbst bauen

Seit v0.2.0 enthält das Windows-Paket der Edge0-Releases (`edge0-chat-<version>-windows-x64.zip`) neben der
Chat-Oberfläche auch den Server `edge0-infer` (mit `serve`) und für 145B/120B `edge0-strata`. Dann entfällt das Bauen
(3.1–3.3 und der Build-Teil von 3.5):

1. Paket von <https://github.com/Edge0-AI/Edge0/releases> laden und gegen den SHA-256-Wert aus den Release-Notes prüfen,
   z. B. für v0.3.0:

   ```powershell
   (Get-FileHash .\edge0-chat-0.3.0-windows-x64.zip -Algorithm SHA256).Hash -eq '3A14C033B8A8334F38E7C54CAEB2335809B3B32A3CC0ADB2EE23499DC2DC2560'
   ```

2. Nach `D:\Edge0\app` entpacken. Den Aufbau beschreibt die `README.md` im Paket; `start.ps1` sucht `edge0-infer.exe`
   unter `-Edge0Dir` an jeder Stelle. Zur Codesignatur unter Windows sagen die Release-Notes nichts – SmartScreen kann
   beim ersten Start warnen.
3. Die Chat-Oberfläche nicht starten.
4. Modell:
   - **145B/120B:** herunterladen wie in 3.5 („Modell …“). Laut Release-Notes zusätzlich nötig: CUDA-Toolkit 13, eine
     NVIDIA-GPU ab Compute Capability 7.5 (RTX 20 bis 50) mit mindestens 16 GiB Grafikspeicher und 20 GiB Arbeitsspeicher.
     Findet `edge0-infer` die mitgelieferte strata nicht selbst, `-StrataDir`/`-StrataPython` von `start.ps1` auf die
     Dateien im Paket setzen.
   - **35B:** Für Windows nennen die Release-Notes keine fertige Modelldatei. Das umgewandelte Modell entsteht wie in 3.4
     mit den Hilfswerkzeugen aus dem Repository – dafür reichen Git und uv, ein C++-Build ist nicht nötig.
5. Weiter mit Phase B: Firewall-Sperre über den ganzen Ordner (`-Edge0Dir D:\Edge0`), dann `start.ps1`.

### 3.1 Werkzeuge

- Git, CMake (≥ 3.21; für 145B/120B ≥ 3.24), Ninja, [uv](https://docs.astral.sh/uv/)
- Visual Studio 2022 oder die Build Tools 2022 mit der Arbeitslast „Desktopentwicklung mit C++“
- für Vulkan: Vulkan SDK (setzt `VULKAN_SDK`, liefert `glslc`)
- nur für 145B/120B: CUDA-Toolkit 13.0–13.2 (setzt `CUDA_PATH`)

Diese Werkzeuge braucht nur das Bauen; der Betrieb nutzt keins davon.

### 3.2 Quellcode auf festem Stand

```powershell
git clone https://github.com/Edge0-AI/Edge0.git C:\src\edge0
git -C C:\src\edge0 rev-parse HEAD     # Stand notieren (für spätere Aktualisierungen)
```

Kurzer Pfad wie `C:\src\edge0` ist wichtig (lange Pfade stören den Shader-Build).

### 3.3 Edge0 35B: Programm bauen

In der **„x64 Native Tools Command Prompt for VS 2022“** `powershell` starten, dann:

```powershell
cd C:\src\edge0\edge0-infer
cmake --preset windows-vulkan          # nur CPU: --preset windows (Ausgabe dann in build\windows\bin)
cmake --build --preset windows-vulkan  # der erste Build übersetzt Shader und dauert lange
build\windows-vulkan\bin\edge0-infer.exe info   # erwartet u. a. „model.0: edge0_35b“
```

Für den Betrieb den Inhalt von `build\windows-vulkan\bin` nach `D:\Edge0\bin` kopieren.

### 3.4 Edge0 35B: Modell holen und umwandeln

```powershell
cd C:\src\edge0\edge0-infer
uv sync --project tools/assets
$env:MOE_SOURCE_35B = "D:\Edge0\quelle\edge0-35b"
$env:MOE_ASSETS_35B = "D:\Edge0\modelle\edge0-35b"
function assets_py { uv run --project tools/assets --offline --no-sync python -I @args }
assets_py tools/assets/edge0_35b/fetch_source.py probe
assets_py tools/assets/edge0_35b/fetch_source.py download --out $env:MOE_SOURCE_35B
assets_py tools/assets/edge0_35b/make_assets.py --source $env:MOE_SOURCE_35B --out $env:MOE_ASSETS_35B
assets_py tools/assets/check_assets.py check $env:MOE_ASSETS_35B
```

Ins Netz gehen nur `uv sync` (Python-Pakete der Hilfswerkzeuge) und `download` (Hugging Face); die übrigen Schritte
laufen mit `--offline`. `assets.tsv` im Modellordner enthält Größe und SHA-256 jeder Datei,
`check` prüft dagegen. Für den Betrieb wird nur `D:\Edge0\modelle\edge0-35b` gebraucht; der Quellordner kann danach
gelöscht werden. Kurztest:

```powershell
D:\Edge0\bin\edge0-infer.exe run --model D:\Edge0\modelle\edge0-35b --device vulkan --user "1+1=" --max-tokens 8
```

### 3.5 Optional: Edge0 145B/120B (NVIDIA, CUDA 13)

Edge0 startet dafür `edge0-strata` als eigenen Prozess. Das gebaute `edge0-infer.exe` aus 3.3 (Preset `windows`
genügt) wird zusätzlich gebraucht. In der **„x64 Native Tools Command Prompt for VS 2022“** (cmd):

```bat
xcopy /E /I C:\src\edge0\edge0-strata D:\Edge0\edge0-strata
cd /d D:\Edge0\edge0-strata
mkdir third_party\llama.cpp
curl.exe -L -o third_party\llama-source.tar.gz https://github.com/ggml-org/llama.cpp/archive/3cf03257f219afbe7334045ff7c6a06ac68c627d.tar.gz
tar -xf third_party\llama-source.tar.gz --strip-components=1 -C third_party\llama.cpp
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_COMPILER="%CUDA_PATH%\bin\nvcc.exe" -DCMAKE_CUDA_ARCHITECTURES=native -DSTRATA_ENABLE_CUDA=ON -DSTRATA_GGML_DIR="%CD%\third_party\llama.cpp"
cmake --build build --target strata
```

Die Python-Umgebung bekommt einen **eigenen** Interpreter im Edge0-Ordner. Grund: Eine `.venv` unter Windows startet
in Wahrheit den Basis-Interpreter, aus dem sie erzeugt wurde. Liegt der außerhalb von `D:\Edge0`, müsste die
Firewall-Sperre ihn treffen – und damit jedes andere Programm, das ihn nutzt (z. B. den LocalAI-Orchestrator selbst).
In PowerShell:

```powershell
cd D:\Edge0\edge0-strata
uv python install 3.12 --install-dir D:\Edge0\python
$py = (Get-ChildItem D:\Edge0\python -Recurse -Filter python.exe | Select-Object -First 1).FullName
uv venv --python $py .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Modell (Q3_K, 40,8 GiB; daneben gibt es `Edge0/Edge0-145B-NVFP4` mit 47,8 GiB), z. B. mit dem Hugging-Face-Werkzeug:

```powershell
uvx --from huggingface_hub hf download Edge0/Edge0-145B-q3k --local-dir D:\Edge0\modelle\Edge0-145B-q3k
D:\Edge0\bin\edge0-infer.exe inspect --model D:\Edge0\modelle\Edge0-145B-q3k --json
```

Der Modellordner muss beschreibbar bleiben: der erste Start legt dort `mtp-q2_0.gguf` an. Findet `strata.exe`
`cublas64_13.dll` nicht, muss `%CUDA_PATH%\bin` im `PATH` stehen (das erledigt `start.ps1` bei `-Device cuda…`).

## 4. Phase B: offline betreiben

### 4.1 Ordner

```text
D:\Edge0\
  bin\                     edge0-infer.exe und DLLs (aus build\windows-vulkan\bin bzw. build\windows\bin)
  app\                     stattdessen: entpacktes Release-Paket (Abschnitt 3.0)
  modelle\edge0-35b\       umgewandeltes 35B-Modell (model.json, assets.tsv, *.gguf)
  modelle\Edge0-145B-q3k\  nur 145B
  edge0-strata\            nur 145B/120B: build\strata.exe, .venv
  python\                  nur 145B/120B: eigener Python-Interpreter
  edge0.key                nur bei Zugriff aus dem LAN (Token)
```

### 4.2 Firewall-Sperre einrichten (einmalig, als Administrator)

```powershell
# Edge0 und Orchestrator auf demselben Rechner (nur Loopback):
powershell -ExecutionPolicy Bypass -File scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0
# Orchestrator auf einem anderen Rechner (hier 192.168.178.50): Port 8000 nur von dort
powershell -ExecutionPolicy Bypass -File scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0 -AllowFrom 192.168.178.50
```

Nach jedem Update von Edge0 (neue oder ersetzte `.exe`) erneut ausführen; alte Regeln der Gruppe „LocalAI Edge0“
werden dabei ersetzt. `-Check` zeigt die Regeln und meldet Programme ohne Sperre, `-Remove` entfernt alles.

Fragt Windows beim ersten Start trotzdem „Zugriff zulassen?“, nichts zulassen und danach `firewall.ps1 -Check`
ausführen: es listet fremde Eingangsregeln für `edge0-infer.exe`. Eine Zulassungsregel aus der Abfrage würde den Port
für beliebige Rechner öffnen, eine Blockierregel (entsteht beim Abbrechen) sperrt den Orchestrator aus – beide entfernen.

### 4.3 Server starten

```powershell
# 35B mit Vulkan, nur für diesen Rechner, 32K Kontext
powershell -ExecutionPolicy Bypass -File scripts\edge0\start.ps1 -Edge0Dir D:\Edge0 -Model D:\Edge0\modelle\edge0-35b -Device vulkan -Context 32768

# für den Orchestrator auf einem anderen Rechner: auf allen Adressen lauschen, mit Token
# (fehlt die Token-Datei, erzeugt das Skript ein zufälliges Token)
powershell -ExecutionPolicy Bypass -File scripts\edge0\start.ps1 -Edge0Dir D:\Edge0 -Model D:\Edge0\modelle\edge0-35b -Device vulkan -Context 32768 -ListenAddress 0.0.0.0 -ApiKeyFile D:\Edge0\edge0.key

# 145B über strata
powershell -ExecutionPolicy Bypass -File scripts\edge0\start.ps1 -Edge0Dir D:\Edge0 -Model D:\Edge0\modelle\Edge0-145B-q3k -Device cuda:0 -Context 32768 -StrataDir D:\Edge0\edge0-strata -StrataPython D:\Edge0\edge0-strata\.venv\Scripts\python.exe
```

Der Server lauscht erst, wenn das Modell geladen und aufgewärmt ist. Die Token-Datei wie ein Passwort behandeln (nur
für dein Konto lesbar) und für den Orchestrator auf dessen Rechner kopieren.

### 4.4 Nachprüfen

```powershell
# alle Verbindungen des Servers: erwartet nur „Listen“ auf Port 8000 und Verbindungen vom Orchestrator
Get-NetTCPConnection -OwningProcess (Get-Process edge0-infer).Id |
    Format-Table LocalAddress, LocalPort, RemoteAddress, RemotePort, State
# Regeln vorhanden und kein Programm ohne Sperre?
powershell -ExecutionPolicy Bypass -File scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0 -Check
```

Wer es genau wissen will: `Set-NetFirewallProfile -All -LogBlocked True` (als Administrator) protokolliert verworfene
Pakete in `%SystemRoot%\System32\LogFiles\Firewall\pfirewall.log` – Versuche von Edge0, nach außen zu verbinden,
stünden dort als `DROP … SEND`.

## 5. Phase C: in LocalAI einrichten

Vorlage: `config/models.edge0.example.json`. Wie immer nur die abweichenden Werte in `config/models.local.json`
(installierte Fassung: Benutzerordner, siehe [Installation](installation.md)). Alle Felder:
[Konfiguration](konfiguration.md#backend-und-edge0).

### 5.1 Alle Rollen über Edge0

```json
{
  "backend": "edge0",
  "edge0": { "url": "http://127.0.0.1:8000" },
  "profiles": [
    { "name": "edge0", "model": "edge0_35b", "contextTokens": 32768, "maxOutputTokens": 8192, "outputUpToContext": true }
  ],
  "roles": {
    "Planner": ["edge0"], "Coder": ["edge0"], "Tester": ["edge0"], "Debugger": ["edge0"], "Reviewer": ["edge0"]
  }
}
```

`model` ist der Alias des Servers (`edge0_35b`, `edge0_145b`, `edge0_120b` oder was mit `--alias` gesetzt wurde).
`contextTokens` höchstens so groß wie `--context` beim Serverstart.

### 5.2 Gemischt: eine Rolle über Edge0, der Rest über Ollama

```json
{
  "ollama": { "url": "http://192.168.178.39:11434", "allowLan": true },
  "edge0": { "url": "http://192.168.178.50:8000", "allowLan": true, "apiKeyFile": "C:\\LocalAI\\edge0.key" },
  "profiles": [
    { "name": "gross", "model": "qwen2.5-coder:14b", "contextTokens": 32768, "maxOutputTokens": 4096 },
    { "name": "edge0", "model": "edge0_35b", "contextTokens": 32768, "maxOutputTokens": 8192, "backend": "edge0" }
  ],
  "roles": { "Reviewer": ["edge0", "gross"] }
}
```

Ohne `backend` gilt für ein Profil der Standard (`"backend"` auf oberster Ebene, sonst `ollama`). Ist Edge0 nicht
erreichbar, nimmt die Rolle das nächste Profil ihrer Liste (hier `gross`); der Aktivitätsfeed zeigt „Edge0 nicht
erreichbar“. Das Token kann statt `apiKeyFile` auch in der Umgebungsvariable `LOCALAI_EDGE0_API_KEY` stehen; im
Klartext in der Konfiguration wird es bewusst nicht angenommen.

### 5.3 Kalibrieren

```powershell
uv run --project src/python python -m localai.orchestrator.calibrate --profile edge0
# Edge0 auf einem anderen Rechner (VRAM dort nicht messbar):
uv run --project src/python python -m localai.orchestrator.calibrate --profile edge0 --ohne-vram-messung
```

Edge0 liefert keine eigenen Zeitmessungen; die Kalibrierung rechnet „Eingabe“ als Zeit bis zur ersten Ausgabe und
„Ausgabe“ als den Rest. Ein Modell-Digest wie bei Ollama gibt es nicht – nach einem Modellwechsel unter demselben Alias
neu kalibrieren.

### 5.4 Was bei Edge0 anders ist

| Thema | Verhalten |
|---|---|
| `options` der Profile | Nur `repeat_penalty` (→ `repetition_penalty`), `top_k`, `top_p`, `seed` werden übertragen; Ollama-spezifische Optionen (`repeat_last_n`, `num_*`, …) entfallen, weil Edge0 unbekannte Felder ablehnt |
| Denkphase | `think: false` (z. B. beim Zusammenfassen der Notizen) sendet `reasoning_effort: "none"`; lehnt der Server das ab, geht es ohne weiter. Je Profil einstellbar mit `reasoningEffort` (z. B. `"none"` oder `"low"`). Die 145B-Modellkarte nennt leere Antworten, wenn die Denkphase das ganze Ausgabebudget aufbraucht – dann `reasoningEffort: "none"` oder `outputUpToContext: true` |
| Kontext | fest beim Serverstart (`--context`); `num_ctx` wird nicht gesendet |
| Modell entladen / `ollama ps` | gibt es nicht; das Modell bleibt geladen, solange der Server läuft |
| Belegt (HTTP 429) | Warten bis `edge0.busyWaitSeconds` (Standard 600), dann Fehler |

## 6. Fehlerbilder

| Meldung | Ursache / Abhilfe |
|---|---|
| „Edge0 ist nicht erreichbar … (edge0-infer serve …)“ | Server läuft nicht oder lädt noch (lauscht erst nach dem Laden); falscher Port; im LAN: `-ListenAddress`, Eingangsregel (`firewall.ps1 -AllowFrom`) |
| „Edge0 bietet das Modell nicht an (… --alias …)“ | `model` im Profil ≠ Alias des Servers. Alias prüfen: `Invoke-RestMethod http://127.0.0.1:8000/v1/models` |
| „Edge0 auf einem anderen Rechner verlangt ein Token“ | `apiKeyFile` bzw. `LOCALAI_EDGE0_API_KEY` fehlt |
| „Edge0 lehnt das Token ab (HTTP 401)“ | Token-Datei auf beiden Rechnern nicht gleich |
| „Edge0 ist belegt … aufgegeben“ | ein anderer Client nutzt den Server; `limits.maxGpuRequests` auf 1 lassen |
| „unknown field …“ | neuere Edge0-Fassung kennt ein Feld nicht mehr – Edge0-Stand (Commit) und Meldung notieren |
| `start.ps1`: „keine Ausgangssperre“ | zuerst `firewall.ps1` (als Administrator) ausführen |

## Quellen

- Edge0-Repository und README: <https://github.com/Edge0-AI/Edge0>
- Releases (Windows-Paket, SHA-256-Werte, Voraussetzungen für 145B/120B): <https://github.com/Edge0-AI/Edge0/releases>
- Bauen und Starten unter Windows/Linux: `edge0-infer/docs/getting-started.md` im Repository
- Server-Schnittstelle (Endpunkte, Streaming, 429, Token): `edge0-infer/docs/local-server.md`
- 145B/120B mit CUDA: `edge0-strata/EDGE0.md`
- Modellkarten: <https://huggingface.co/Edge0/Edge0-35B-A3B-preview>, <https://huggingface.co/Edge0/Edge0-145B-q3k>
