#!/usr/bin/env bash
# M10: Baut Broker und Linux-Oberfläche (Avalonia) nach out/linux – ohne WPF, ohne Windows-Pakete.
set -euo pipefail
cd "$(dirname "$0")/../.."
uv sync --project src/python
dotnet publish src/dotnet/LocalAi.Desktop.Linux -c Release -o out/linux -nologo
echo "Fertig: out/linux/LocalAi.Desktop.Linux (Start: scripts/linux/start.sh)"
