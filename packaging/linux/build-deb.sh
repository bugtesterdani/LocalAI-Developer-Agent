#!/usr/bin/env bash
# Baut das Debian-Paket aus dem Installationsordner (packaging/package.py stage --platform linux).
# Aufruf: packaging/linux/build-deb.sh <Installationsordner> <Symbole> <Version> <Ausgabe.deb>
#
# Installiert nach /opt/localai, Startbefehl /usr/bin/localai, Eintrag im Anwendungsmenü. Abhängigkeiten (QEMU,
# dosfstools, mtools, bubblewrap und die Bibliotheken von .NET/Avalonia) holt apt: sudo apt install ./<paket>.deb
set -euo pipefail
stage="$1"; icons="$2"; version="$3"; output="$4"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

mkdir -p "$root/opt" "$root/usr/bin" "$root/usr/share/applications" "$root/usr/share/icons/hicolor/256x256/apps" \
         "$root/usr/share/doc/localai-developer-agent" "$root/DEBIAN"
cp -a "$stage" "$root/opt/localai"
install -m 0755 "$here/localai" "$root/usr/bin/localai"
install -m 0644 "$here/localai.desktop" "$root/usr/share/applications/localai.desktop"
install -m 0644 "$icons/localai.png" "$root/usr/share/icons/hicolor/256x256/apps/localai.png"
install -m 0644 "$stage/README.md" "$root/usr/share/doc/localai-developer-agent/README.md"
install -m 0755 "$here/postinst" "$root/DEBIAN/postinst"

# Rechte: alles lesbar, Programme und Skripte ausführbar, nichts für andere beschreibbar.
chmod -R u=rwX,go=rX "$root/opt/localai"
find "$root/opt/localai" -type f \( -name '*.sh' -o -path '*/runtime/python/bin/*' \) -exec chmod 0755 {} +
for exe in LocalAi.Desktop.Linux LocalAi.Broker.Host; do chmod 0755 "$root/opt/localai/app/$exe"; done

size="$(du -sk "$root" | cut -f1)"
sed -e "s/__VERSION__/$version/" -e "s/__SIZE__/$size/" "$here/control" > "$root/DEBIAN/control"
dpkg-deb --root-owner-group -Zxz --build "$root" "$output"
echo "Paket: $output"
