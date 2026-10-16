#!/usr/bin/env bash
# Baut und testet alles außer der WPF-Oberfläche (Linux/macOS, z. B. CI).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> 1/4 Generierte Verträge aktuell?"
python3 tools/codegen/generate.py --check
echo "==> 2/4 C# bauen (ohne WPF)"
for p in LocalAi.Broker.Host LocalAi.Tests; do dotnet build "src/dotnet/$p" -c Debug -nologo -v q; done
echo "==> 3/4 C#-Tests"
dotnet test src/dotnet/LocalAi.Tests --no-build
echo "==> 4/4 Python-Tests"
(cd src/python && uv run pytest -q)
echo "Alles grün."
