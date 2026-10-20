#!/usr/bin/env bash
# M10: Lädt Debian-Abbild, Python für Linux, Wheels (und mit --with-qemu QEMU für das Betriebssystem-Debugging in der VM).
# Aufruf: scripts/linux/get-prerequisites.sh ~/LocalAi/linux [--with-qemu] [--skip-image]
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ $# -lt 1 ]; then
    echo "Aufruf: $0 <Zielordner> [--with-qemu] [--skip-image]" >&2
    exit 2
fi
target="$1"; shift
uv sync --project src/python
exec uv run --no-sync --project src/python python tools/prereq/linux_prerequisites.py --target "$target" "$@"
