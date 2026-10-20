#!/usr/bin/env bash
# M10: Startet die Oberfläche unter Linux; sie startet Broker und Orchestrator selbst (config/localai.json).
set -euo pipefail
cd "$(dirname "$0")/../.."
app=out/linux/LocalAi.Desktop.Linux
if [ ! -x "$app" ]; then
    scripts/linux/build.sh
fi
if [ ! -f config/localai.json ]; then
    echo "config/localai.json fehlt – Vorlage config/localai.linux.json kopieren und anpassen." >&2
    exit 1
fi
exec "$app" "$@"
