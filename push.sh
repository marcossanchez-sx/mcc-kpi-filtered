#!/usr/bin/env bash
# Sube el proyecto a GitHub (Linux/macOS/Git Bash). En PowerShell usa .\push.ps1
set -euo pipefail
cd "$(dirname "$0")"
REMOTE="https://github.com/marcossanchez-sx/mcc-kpi-filtered.git"

git remote get-url origin >/dev/null 2>&1 && git remote set-url origin "$REMOTE" \
  || git remote add origin "$REMOTE"

echo "Comprobando que no se suba ningún dato operativo..."
LEAKS=$(git ls-files | grep '\.csv$' | grep -v example || true)
if [ -n "$LEAKS" ]; then
  echo "ABORTADO: CSV con datos reales en el commit:"; echo "$LEAKS"; exit 1
fi
echo "  correcto: sólo plantillas .example.csv"

git fetch origin
git push -u --force-with-lease origin main
echo "Listo: https://github.com/marcossanchez-sx/mcc-kpi-filtered"
