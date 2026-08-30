#!/usr/bin/env bash
# Sovereign AI Substrate PoC — one-click runner (Linux/macOS)
#   ./run_substrate.sh            mock mode (no keys needed)
#   ./run_substrate.sh sarvam     live Sarvam composition + judge
set -e
cd "$(dirname "$0")"
PROVIDER="${1:-}"   # empty = use .env LLM_PROVIDER

echo "[1/5] venv..."
[ -f .venv/bin/python ] || python3 -m venv .venv
source .venv/bin/activate

echo "[2/5] deps..."
python -c "import fastapi, uvicorn, pydantic" 2>/dev/null || {
  pip install -q --upgrade pip && pip install -q -r requirements.txt; }

echo "[3/5] corpus index..."
[ -f data/manifests/CURRENT ] && echo "      found - skipping" \
  || python -m backend.substrate.ingest

echo "[4/5] event store..."
[ -f data/skilling_events.db ] && echo "      found - skipping" \
  || python -m backend.substrate.events >/dev/null

echo "[5/5] starting (provider: $PROVIDER)..."
export SUBSTRATE_RAG=true APP_ENV=development AUTO_SEED_CORPORA=false
[ -n "$PROVIDER" ] && export LLM_PROVIDER="$PROVIDER"
echo "------------------------------------------------------------"
echo " Demo console : http://localhost:8000/substrate-demo"
echo " Logins       : meena/learner-demo  rajesh/officer-demo"
echo "                iyer/sme-demo       admin/admin-demo"
echo "------------------------------------------------------------"
python -m uvicorn backend.main:app --port 8000
