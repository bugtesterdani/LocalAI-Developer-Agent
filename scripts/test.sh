#!/usr/bin/env bash
# Baut und testet alles außer der WPF-Oberfläche (Linux/macOS, z. B. CI) – einschließlich der Linux-Oberfläche (M10).
set -euo pipefail
cd "$(dirname "$0")/.."
# Python-Umgebung zuerst: C#-Tests starten Fake-Agent und Worker mit "uv run --no-sync" und lesen src/python/.venv.
uv sync --project src/python
echo "==> 1/5 Generierte Verträge aktuell?"
python3 tools/codegen/generate.py --check
echo "==> 2/5 C# bauen (ohne WPF, mit Avalonia-Oberfläche)"
for p in LocalAi.Broker.Host LocalAi.Tests LocalAi.Desktop.Linux LocalAi.Desktop.Linux.Tests; do dotnet build "src/dotnet/$p" -c Debug -nologo -v q; done
echo "==> 3/5 C#-Tests"
dotnet test src/dotnet/LocalAi.Tests --no-build
echo "==> 4/5 Oberflächentests Linux (ohne Bildschirm)"
dotnet test src/dotnet/LocalAi.Desktop.Linux.Tests --no-build
echo "==> 5/5 Python-Tests (QEMU-Ende-zu-Ende nur mit qemu, dosfstools, mtools und Test-Kern)"
(cd src/python && uv run pytest -q)
echo "Alles grün."
