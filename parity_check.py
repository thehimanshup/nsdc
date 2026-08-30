"""Engine parity harness — Phase 6f ship-gate.

Replays the SAME scripted citizen turns through the legacy engine and the
LangGraph engine and diffs the *structural* outcomes (was a record created?
which department/category? did the agent reply? was consent requested?). We
do NOT diff reply wording — different phrasing is expected; we diff behaviour.

Each engine runs in its OWN subprocess with a fresh DATA_DIR (the store is a
module singleton, so isolation needs separate processes).

Usage:
    LLM_PROVIDER=mock python parity_check.py            # runs both, diffs, exits 0/1
    python parity_check.py run                          # internal: run one engine, print JSON
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

# Avoid Windows cp1252 encode/decode failures for emoji/log output emitted by
# child engines or by the summary table.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# (conv agent, text). Chosen to exercise: record creation, dedupe, plain Q&A,
# a second department record, and a consent-gated tool.
SCRIPT = [
    ("water",   "there is a big water leak on North Mada Street for two days"),
    ("water",   "the leak is still there, any update on my complaint?"),
    ("health",  "what is the ambulance number?"),
    ("cmo",     "I want to file a grievance about uncollected garbage"),
    ("revenue", "I need my patta land record fetched"),
]


def _run_one_engine() -> dict:
    """Run the script for the engine configured in env; return outcomes."""
    from fastapi.testclient import TestClient
    import backend.main as m
    from backend.store import store

    out = {"engine": os.environ.get("ORCHESTRATOR_ENGINE", "legacy"), "turns": []}
    with TestClient(m.app) as c:
        cid = c.post("/api/v1/auth/init",
                     json={"msisdn": "9876500077", "state_code": "TN"}).json()["citizenId"]
        for agent, text in SCRIPT:
            before = c.get(f"/api/v1/citizens/{cid}/records").json()["count"]
            c.post(f"/api/v1/citizens/{cid}/conversations/{agent}/messages",
                   json={"text": text})
            # Legacy runs tool + a follow-up LLM stream as two sequential steps,
            # so allow enough settle time for both engines to fully finish.
            time.sleep(2.0 if os.environ.get("LLM_PROVIDER") == "mock" else 4.0)
            recs = c.get(f"/api/v1/citizens/{cid}/records").json()
            after = recs["count"]
            conv = store.conversations.get(f"{cid}:{agent}", [])
            replied = any(x.role == "agent" and x.type == "text" for x in conv)
            consent = any((x.extra or {}).get("isConsentPrompt") for x in conv)
            newest = recs["records"][0] if recs["count"] else {}
            out["turns"].append({
                "agent": agent,
                "record_created": after > before,
                "record_count": after,
                "new_dept": newest.get("department") if after > before else None,
                "new_category": newest.get("category") if after > before else None,
                "replied": replied,
                "consent_requested": consent,
            })
    return out


def _spawn(engine: str) -> dict:
    data = tempfile.mkdtemp(prefix=f"parity_{engine}_")
    env = {**os.environ, "ORCHESTRATOR_ENGINE": engine, "DATA_DIR": data,
           "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "mock"),
           "SARVAM_API_KEY": os.environ.get("SARVAM_API_KEY", "") if os.environ.get("LLM_PROVIDER") != "mock" else "",
           "PUSH_DEMO_ENABLED": "false"}
    r = subprocess.run([sys.executable, __file__, "run"], env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    # The JSON result is the last line of stdout.
    for line in reversed(r.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"engine {engine} produced no result.\nSTDERR:\n{r.stderr[-800:]}")


def main() -> int:
    legacy = _spawn("legacy")
    graph = _spawn("graph")
    keys = ("agent", "record_created", "new_dept", "new_category", "replied", "consent_requested")
    print("\n=== ENGINE PARITY SWEEP (legacy vs graph, mock) ===\n")
    print(f"{'turn/agent':<10} {'record':<8} {'dept':<9} {'category':<14} {'reply':<6} {'consent':<8} {'match'}")
    all_match = True
    for i, (lt, gt) in enumerate(zip(legacy["turns"], graph["turns"])):
        match = all(lt.get(k) == gt.get(k) for k in keys[1:])
        all_match &= match
        rc = "yes" if lt["record_created"] else "—"
        print(f"{lt['agent']:<10} {rc:<8} {str(lt['new_dept'] or '—'):<9} "
              f"{str(lt['new_category'] or '—'):<14} {str(lt['replied']):<6} "
              f"{str(lt['consent_requested']):<8} {'✅' if match else '❌ DIFF'}")
        if not match:
            print(f"    legacy={ {k: lt.get(k) for k in keys} }")
            print(f"    graph ={ {k: gt.get(k) for k in keys} }")
    print("\n=== " + ("PARITY PASS ✅ — graph matches legacy" if all_match
                       else "PARITY FAIL ❌ — see diffs above") + " ===\n")
    return 0 if all_match else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        print(json.dumps(_run_one_engine()))
    else:
        sys.exit(main())
