#!/usr/bin/env bash
# ========================================================
# Quick-start script for macOS / Linux.
# .env loading is handled inside Python (backend/config.py)
# via python-dotenv.
# ========================================================
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

echo "Installing dependencies..."
python -m pip install --quiet --disable-pip-version-check -r requirements.txt

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo ""
echo "============================================================"
echo " Government Services Multi-Agent Backend (Phase 1)"
echo " Open in browser:  http://${HOST}:${PORT}/"
echo " Press Ctrl+C to stop."
echo "============================================================"
echo ""

exec python -m uvicorn backend.main:app --reload --host "${HOST}" --port "${PORT}"
