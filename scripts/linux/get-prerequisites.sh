#!/usr/bin/env bash
# M10: Lädt Debian-Abbild, Python für Linux, Wheels (und mit --with-qemu QEMU für das Betriebssystem-Debugging in der VM).
# Aufruf: scripts/linux/get-prerequisites.sh ~/LocalAi/linux [--with-qemu] [--skip-image]
# Läuft im Repository (über uv) und in der installierten Fassung (mitgeliefertes Python unter runtime/python).
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ $# -lt 1 ]; then
    echo "Aufruf: $0 <Zielordner> [--with-qemu] [--skip-image]" >&2
    exit 2
fi
target="$1"; shift
if [ -x runtime/python/bin/python3 ]; then
    exec runtime/python/bin/python3 tools/prereq/linux_prerequisites.py --target "$target" "$@"
fi
uv sync --project src/python
exec uv run --no-sync --project src/python python tools/prereq/linux_prerequisites.py --target "$target" "$@"
