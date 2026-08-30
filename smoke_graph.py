"""Smoke test for the LangGraph engine — run with your real Sarvam key.

    # mock (no network, always works):
    LLM_PROVIDER=mock ORCHESTRATOR_ENGINE=graph python smoke_graph.py
    # live (uses SARVAM_API_KEY from your .env / env):
    ORCHESTRATOR_ENGINE=graph python smoke_graph.py

It boots the app in-process (no separate server), runs a few turns through the
graph engine, and checks that: the backend is healthy, a complaint turn creates
a real record (real LLM function-calling in live mode), and a plain question
gets a reply. Exits non-zero on failure so you can wire it into CI.
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("ORCHESTRATOR_ENGINE", "graph")

from fastapi.testclient import TestClient   # noqa: E402
import backend.main as m                    # noqa: E402
from backend.store import store             # noqa: E402
from backend.config import settings         # noqa: E402


def _ok(cond, label):
    print(("  ✅ " if cond else "  ❌ ") + label)
    return cond


def main() -> int:
    mode = "MOCK" if settings.mock_mode else "LIVE (Sarvam)"
    print(f"\n=== Graph engine smoke test — engine={settings.orchestrator_engine}, LLM={mode} ===\n")
    passed = True
    with TestClient(m.app) as c:
        h = c.get("/api/health").json()
        passed &= _ok(h.get("status") == "ok", f"health ok (mode={h.get('mode')}, agents={h.get('agents')}, tools={h.get('tools')})")

        cid = c.post("/api/v1/auth/init",
                     json={"msisdn": "9876500009", "state_code": "TN"}).json()["citizenId"]
        passed &= _ok(bool(cid), f"auth/init → {cid}")

        # 1) A complaint that should trigger a real records.create tool call.
        c.post(f"/api/v1/citizens/{cid}/conversations/water/messages",
               json={"text": "there is a big water leak on North Mada Street for two days"})
        time.sleep(2.5 if not settings.mock_mode else 0.8)
        recs = c.get(f"/api/v1/citizens/{cid}/records").json()
        passed &= _ok(recs["count"] >= 1,
                      f"complaint created a record: count={recs['count']} "
                      f"({recs['records'][0]['recordId'] if recs['count'] else '—'})")
        ar = [x for x in store.conversations.get(f"{cid}:water", []) if x.role == "agent"]
        eng = (ar[-1].extra or {}).get("engine") if ar else None
        passed &= _ok(bool(ar), f"agent replied (engine={eng}): "
                                f"{ar[-1].text[:60] if ar else '—'!r}")

        # 2) A plain question (no tool) should still get a reply.
        c.post(f"/api/v1/citizens/{cid}/conversations/health/messages",
               json={"text": "what is the ambulance number?"})
        time.sleep(2.5 if not settings.mock_mode else 0.6)
        har = [x for x in store.conversations.get(f"{cid}:health", []) if x.role == "agent"]
        passed &= _ok(bool(har), f"health Q&A replied: {har[-1].text[:60] if har else '—'!r}")

    print("\n=== " + ("ALL CHECKS PASSED ✅" if passed else "SOME CHECKS FAILED ❌") + " ===\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
