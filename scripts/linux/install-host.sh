#!/usr/bin/env bash
# M10: Richtet einen Linux-Host für LocalAi ein (Debian/Ubuntu: apt, Fedora: dnf, Arch: pacman).
#   QEMU (qemu-system-x86, qemu-img), dosfstools + mtools (Seed-Laufwerk ohne root), bubblewrap (Webabruf isoliert),
#   Benutzer in die Gruppe kvm (QEMU mit KVM statt reiner Emulation). .NET 10 SDK und uv werden nur geprüft.
# Braucht sudo nur für die Paketinstallation und die Gruppe; LocalAi selbst läuft danach ohne root.
set -euo pipefail
if command -v apt-get >/dev/null; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-system-x86 qemu-utils dosfstools mtools bubblewrap
elif command -v dnf >/dev/null; then
    sudo dnf install -y qemu-system-x86-core qemu-img dosfstools mtools bubblewrap
elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm qemu-base dosfstools mtools bubblewrap
else
    echo "Paketverwaltung nicht erkannt – bitte qemu-system-x86_64, qemu-img, mkfs.fat, mcopy und bwrap selbst installieren." >&2
    exit 1
fi
if [ -e /dev/kvm ]; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx kvm; then
        sudo usermod -aG kvm "$USER"
        echo "Benutzer $USER ist jetzt in der Gruppe kvm – einmal ab- und wieder anmelden."
    fi
else
    echo "WARNUNG: /dev/kvm fehlt (Virtualisierung im BIOS/UEFI aus?) – QEMU emuliert dann, das ist sehr langsam." >&2
fi
missing=0
for tool in dotnet uv; do
    if ! command -v "$tool" >/dev/null; then
        echo "FEHLT: $tool (dotnet: .NET 10 SDK, uv: https://docs.astral.sh/uv/)" >&2
        missing=1
    fi
done
if command -v dotnet >/dev/null && ! dotnet --list-sdks | grep -q '^10\.'; then
    echo "FEHLT: .NET 10 SDK (dotnet --list-sdks)" >&2
    missing=1
fi
[ $missing -eq 0 ] && echo "Host bereit. Weiter: scripts/linux/get-prerequisites.sh <Zielordner>"
exit $missing
