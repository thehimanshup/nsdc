#!/usr/bin/env python3
"""Phase-A preflight — one command that reports exactly what is enabled.

Run on the machine that will host the demo:
    python preflight_substrate.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATA_DIR", "data")

OK, WARN, FAIL = "✅", "🟡", "❌"
rows = []


def check(name, status, detail, action=""):
    rows.append((status, name, detail, action))


# --- A1: Sarvam key ---------------------------------------------------------
key = ""
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("SARVAM_API_KEY=") and len(line.split("=", 1)[1].strip()) > 10:
            key = line.split("=", 1)[1].strip()
if not key:
    check("Sarvam API key", FAIL, "not set in .env", "set SARVAM_API_KEY, then re-run")
else:
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.sarvam.ai/v1/chat/completions",
            data=json.dumps({"model": "sarvam-m", "max_tokens": 4,
                             "messages": [{"role": "user", "content": "OK?"}]}).encode(),
            headers={"Content-Type": "application/json", "api-subscription-key": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            check("Sarvam API key", OK, f"live (HTTP {r.status}) — set LLM_PROVIDER=sarvam",
                  "judge + synthesised answers now available")
    except Exception as e:
        check("Sarvam API key", WARN, f"present but not reachable: {str(e)[:80]}",
              "check network/quota; mock mode still works")

# --- A3: services ------------------------------------------------------------
try:
    import urllib.request
    with urllib.request.urlopen(os.getenv("QDRANT_URL", "http://localhost:6333")
                                + "/collections", timeout=4) as r:
        check("Qdrant (vector leg)", OK, "reachable",
              "run: python -m backend.substrate.ingest --qdrant")
except Exception:
    check("Qdrant (vector leg)", WARN, "not running — BM25-only retrieval",
          "docker compose -f docker-compose.substrate.yml up -d")
try:
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                             auth=("neo4j", os.getenv("NEO4J_PASSWORD", "substrate-dev-pass")))
    d.verify_connectivity(); d.close()
    check("Neo4j (KG leg)", OK, "reachable", "run: python backend/substrate/kg/loader.py")
except Exception:
    check("Neo4j (KG leg)", WARN, "not running — KG pathway leg disabled",
          "docker compose up -d; pip install neo4j")

# --- A2: corpus ----------------------------------------------------------------
import csv
reg = Path("corpus/SOURCE_REGISTER.csv")
if reg.exists():
    rows_reg = list(csv.DictReader(reg.open(encoding="utf-8-sig")))
    seed = [r["doc_id"] for r in rows_reg if r["status"] == "SEED"]
    to_dl = [r["doc_id"] for r in rows_reg if r["status"] == "TO_DOWNLOAD"]
    real = [r for r in rows_reg if r["status"] == "DOWNLOADED"]
    st = OK if not seed and not to_dl else WARN
    check("Corpus", st,
          f"{len(real)} official, {len(seed)} SEED, {len(to_dl)} to download",
          "replace SEED docs with official PDFs, set status=DOWNLOADED, re-ingest" if seed or to_dl else "")
else:
    check("Corpus register", FAIL, "missing", "restore corpus/SOURCE_REGISTER.csv")

# --- index/manifest/events ------------------------------------------------------
cur = Path("data/manifests/CURRENT")
check("Index manifest", OK if cur.exists() else FAIL,
      cur.read_text().strip() if cur.exists() else "no index built",
      "" if cur.exists() else "python -m backend.substrate.ingest")
db = Path("data/skilling_events.db")
if db.exists():
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("Event store", OK, f"{n} synthetic events", "")
else:
    check("Event store", WARN, "empty", "python -m backend.substrate.events")

# --- gold set / judge -------------------------------------------------------------
gold = Path("evals/gold_v1.jsonl")
check("Gold eval set", OK if gold.exists() else FAIL,
      f"{len(gold.read_text(encoding='utf-8').splitlines())} items" if gold.exists() else "missing", "")
try:
    from backend.substrate.judge import stats
    s = stats("data")
    check("Groundedness judge", OK if s.get("scored") else WARN,
          json.dumps(s), "" if s.get("scored") else "activates automatically with live LLM")
except Exception as e:
    check("Groundedness judge", FAIL, str(e)[:80], "")

# --- report ---------------------------------------------------------------------
print("\n=== SUBSTRATE PREFLIGHT ===")
for status, name, detail, action in rows:
    print(f" {status} {name:<24} {detail}")
    if action:
        print(f"    ↳ {action}")
blockers = [r for r in rows if r[0] == FAIL]
print(f"\n {'❌ ' + str(len(blockers)) + ' blocker(s)' if blockers else '✅ no blockers'} — "
      f"{sum(1 for r in rows if r[0]==WARN)} warning(s)")
print(" Demo runbook: DEMO_SCRIPT.md · full status: SUBSTRATE_POC_STATUS.md")
