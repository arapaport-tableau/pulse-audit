#!/usr/bin/env bash
# PulseAudit one-line install + run.
# Creates a venv, installs requirements, starts the app.

set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing requirements…"
pip install --quiet -r requirements.txt

echo
echo "Starting PulseAudit at http://localhost:${PORT:-5050}"
echo "Press Ctrl+C to stop."
echo

python3 app.py
