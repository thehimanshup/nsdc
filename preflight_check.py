"""Pre-demo GO/NO-GO check — run against the RUNNING server, minutes before
the client demo. Verifies the server is healthy, all pages load, the catalogs
are populated, and a live chat turn actually creates a trackable record.

    python preflight_check.py                  # checks http://127.0.0.1:8000
    BASE=http://127.0.0.1:8000 python preflight_check.py

Exits 0 (GO) / 1 (NO-GO).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
results = []

# Windows terminals often default to cp1252, which cannot encode the
# check-mark/cross symbols used by this script. Keep the operator-facing output
# readable without requiring callers to remember PYTHONIOENCODING=utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def check(label, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"error: {e}"
    results.append((ok, label, detail))
    print(("  ✅ " if ok else "  ❌ ") + f"{label:<42} {detail}")
    return ok


def main() -> int:
    print(f"\n=== PRE-DEMO CHECK — {BASE} ===\n")
    # trust_env=False: never route a localhost preflight through a proxy.
    c = httpx.Client(base_url=BASE, timeout=10, trust_env=False)

    def health():
        j = c.get("/api/health").json()
        return j.get("status") == "ok", f"mode={j.get('mode')} agents={j.get('agents')} tools={j.get('tools')}"
    check("backend health", health)

    for path, label in [("/", "citizen chat simulator  /"),
                        ("/services", "citizen services portal /services"),
                        ("/admin/", "admin console /admin/"),
                        ("/admin/ops", "officer ops console /admin/ops")]:
        check(f"page loads: {label}",
              lambda p=path: (c.get(p).status_code == 200, f"HTTP {c.get(p).status_code}"))

    check("agents (expect 11)",
          lambda: (len(c.get("/api/v1/agents").json()["agents"]) == 11,
                   f"{len(c.get('/api/v1/agents').json()['agents'])} agents"))
    check("scheme families (expect 6)",
          lambda: (len(c.get("/api/v1/schemes/families").json()["families"]) == 6,
                   f"{len(c.get('/api/v1/schemes/families').json()['families'])} families"))
    check("projects present",
          lambda: (c.get("/api/v1/projects?state=TN").json()["count"] > 0,
                   f"{c.get('/api/v1/projects?state=TN').json()['count']} TN projects"))
    check("seeded demo records present",
          lambda: (c.get("/api/v1/admin/records").json()["stats"]["total"] > 0,
                   f"{c.get('/api/v1/admin/records').json()['stats']['total']} records in queue"))
    check("seeded citizen has records",
          lambda: (_citizen_records(c) >= 5, f"{_citizen_records(c)} records for 9000000001"))

    # Live functional check: a throwaway citizen files a complaint → record.
    def live_turn():
        # Use a fresh throwaway number each run; otherwise the dedupe/idempotency
        # layer can correctly ignore the same test complaint and make preflight
        # look flaky on repeated runs.
        msisdn = f"99999{int(time.time() * 1000) % 100000:05d}"
        cid = c.post("/api/v1/auth/init", json={"msisdn": msisdn, "state_code": "TN"}).json()["citizenId"]
        before = c.get(f"/api/v1/citizens/{cid}/records").json()["count"]
        c.post(f"/api/v1/citizens/{cid}/conversations/water/messages",
               json={"text": "there is a water leak on my street"})
        time.sleep(2.5)
        after = c.get(f"/api/v1/citizens/{cid}/records").json()["count"]
        return after > before, f"record created: {before} → {after}"
    check("live chat → creates a record", live_turn)

    n_fail = sum(1 for ok, *_ in results if not ok)
    print("\n=== " + ("GO ✅ — demo-ready" if n_fail == 0
                       else f"NO-GO ❌ — {n_fail} check(s) failed") + " ===\n")
    return 0 if n_fail == 0 else 1


def _citizen_records(c) -> int:
    cid = c.post("/api/v1/auth/init",
                 json={"msisdn": "9000000001", "state_code": "TN"}).json()["citizenId"]
    return c.get(f"/api/v1/citizens/{cid}/records").json()["count"]


if __name__ == "__main__":
    sys.exit(main())
