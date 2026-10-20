#!/bin/bash
# Provisionierung des Linux-Gasts (M8), Teil 1 – läuft einmal als root über cloud-init (runcmd), ohne Netzwerk.
# Das Seed-Laufwerk (CIDATA) ist schreibgeschützt unter /mnt/localai-seed eingebunden.
#   1. hyperv-daemons installieren (KVP-Datenaustausch, Copy-VMFile), mit -WithQemu auch QEMU (M9)
#   2. Python für Linux nach /opt/localai/python entpacken, Wheels und Worker einspielen
#   3. Sitzungsschlüssel, Konfiguration und Toolchains nach /var/lib/localai (nur root)
#   4. Teil 2 in Python: localai.guest.provision (Netzwerkprüfung, Toolchains, Worker-Dienst, Bericht)
set -u
umask 022
SEED=/mnt/localai-seed/localai
ROOT=/var/lib/localai
OPT=/opt/localai
LOG=/var/log/localai-provision.log
echo "[$(date +%T)] Provisionierung startet"

fail() {
    echo "[$(date +%T)] FEHLER: $*"
    # Ohne Python kein Bericht – der Host meldet nach seiner Zeitgrenze den letzten Fortschritt.
    exit 1
}

files_of() {
    # prereq/files.txt (vom Host geschrieben): Zeilen "<art> <relativer Pfad>", z. B. "deb debs/hyperv-daemons_….deb".
    sed -n "s/^$1 \([A-Za-z0-9][A-Za-z0-9._+~\/-]*\)\r\{0,1\}$/\1/p" "$SEED/prereq/files.txt"
}

mkdir -p "$ROOT" "$OPT" || fail "Ordner nicht anlegbar"
chmod 0700 "$ROOT"

# 1 – hyperv-daemons (offline, ohne Abhängigkeitsauflösung)
# Alle Pakete in einem Aufruf: dpkg ordnet Abhängigkeiten untereinander selbst (z. B. QEMU samt Bibliotheken, M9).
debs=()
for deb in $(files_of deb); do debs+=("$SEED/prereq/$deb"); done
if [ ${#debs[@]} -gt 0 ]; then
    echo "dpkg -i (${#debs[@]} Pakete)"
    DEBIAN_FRONTEND=noninteractive dpkg -i "${debs[@]}" || fail "Pakete ließen sich nicht installieren (Abhängigkeiten?)"
fi

# 2 – Python und Worker
python_archive=$(files_of python | head -n 1)
[ -n "$python_archive" ] || fail "python fehlt in prereq/files.txt"
rm -rf "$OPT/python"
tar -xzf "$SEED/prereq/$python_archive" -C "$OPT" || fail "Python-Archiv ließ sich nicht entpacken"
PY="$OPT/python/bin/python3"
[ -x "$PY" ] || fail "$PY fehlt nach dem Entpacken"
SITE=$("$PY" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') || fail "Python startet nicht"
for wheel in $(files_of wheel); do
    "$PY" -I -m zipfile -e "$SEED/prereq/$wheel" "$SITE" || fail "Wheel $wheel ließ sich nicht entpacken"
done
rm -rf "$SITE/localai"
cp -r "$SEED/pkg/localai" "$SITE/localai" || fail "Worker ließ sich nicht kopieren"

# 3 – Schlüssel, Konfiguration, Toolchains
install -m 0600 "$SEED/session.key" "$ROOT/session.key" || fail "Sitzungsschlüssel fehlt"
install -m 0600 "$SEED/guest-config.json" "$ROOT/guest-config.json"
install -m 0700 "$SEED/provision.sh" "$ROOT/provision.sh"
rm -rf "$ROOT/toolchain-src"
if [ -d "$SEED/toolchain-src" ]; then
    cp -r "$SEED/toolchain-src" "$ROOT/toolchain-src" || fail "Toolchains ließen sich nicht kopieren"
fi

# 4 – Rest in Python (schreibt den Bericht, auch bei Fehlern)
echo "[$(date +%T)] Teil 2 (Python) startet"
"$PY" -I -m localai.guest.provision --seed /mnt/localai-seed
echo "[$(date +%T)] Provisionierung beendet (Exitcode $?), Protokoll: $LOG"
