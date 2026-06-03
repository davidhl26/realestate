#!/usr/bin/env bash
# Flip Board launcher — creates a venv on first run, installs deps, launches app.
set -e

cd "$(dirname "$0")"

VENV=".venv"
PYTHON="${PYTHON:-python3}"

# 1) Ensure venv exists
if [ ! -d "$VENV" ]; then
  echo "[setup] Creating virtual environment..."
  $PYTHON -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 2) Install deps (idempotent — pip skips already-installed)
echo "[setup] Installing dependencies..."
pip install --quiet --disable-pip-version-check --upgrade pip
pip install --quiet --disable-pip-version-check -r requirements.txt

# 3) Launch
echo "[run] Launching Flip Board..."
exec python3 app.py
