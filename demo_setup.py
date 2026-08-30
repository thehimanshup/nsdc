"""Demo data seeder — makes every screen look alive for a client demo.

Run this ONCE, BEFORE starting the server, against the same DATA_DIR the
server uses (default ./data). It creates a demo citizen with records across
the full lifecycle plus a scheme application and a project-linked complaint,
and a second citizen so the officer queue looks busy.

    # clean slate + seed:
    python demo_setup.py --reset
    # then start the server and run preflight_check.py

`--reset` wipes records.json + store.json first so you start fresh. Without
it, the seed is added to whatever exists.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

DATA = os.environ.get("DATA_DIR", "./data")


def _reset():
    for f in ("records.json", "store.json"):
        p = Path(DATA) / f
        try:
            if p.exists():
                p.unlink()
                print(f"  reset: removed {p}")
        except Exception as e:
            print(f"  reset: could not remove {p}: {e}")


async def _seed():
    from backend.store import store
    from backend.records import service as rsvc
    from backend.records.store import records_store

    # ---- Primary demo citizen: Meena, Chennai (TN) ----
    cid = store.get_or_create_citizen("9000000001")
    store.set_citizen_state(cid, "TN", "ta-IN")
    store.citizens[cid]["profile"] = {
        "gender": "female", "age": 34, "is_pregnant_or_lactating": True,
        "annual_income": 90000, "state_code": "TN", "area": "urban",
        "owns_pucca_house": False,
    }
    store._persist()

    def mk(**kw):
        return rsvc.create_record(citizen_id=cid, msisdn="9000000001",
                                  state_code="TN", channel="simulator",
                                  lang="ta-IN", **kw)

    # 1) Fresh water leak — sits at ASSIGNED L1
    mk(kind="grievance", department_id="water", category="leak",
       title="Water leak on North Mada Street, Mylapore",
       description="Pipe leaking near house no. 21 for two days.")

    # 2) No-supply complaint that has been ESCALATED to L2 (shows the ladder)
    esc = mk(kind="grievance", department_id="water", category="no_supply",
             title="No water supply in Ward 142 since morning",
             description="Entire street has had no supply since 6 AM.")
    await rsvc.escalate(esc, reason="Demo: SLA breached at L1")

    # 3) CMO grievance marked RESOLVED → the citizen sees a feedback card
    res = mk(kind="grievance", department_id="cmo", category="general",
             title="Uncollected garbage near bus stand",
             description="Garbage not cleared for a week.")
    await rsvc.transition(res, to_status="RESOLVED", actor="officer",
                          action="resolved", note="Cleared by sanitation team.",
                          notify=False)

    # 4) Scheme application (PMMVY) — SUBMITTED
    mk(kind="scheme_application", department_id="wcd", category="scheme.application",
       title="Application: PM Matru Vandana Yojana (PMMVY)",
       description="Maternity benefit application.", scheme_id="pmmvy",
       initial_status="SUBMITTED",
       extra={"scheme_name": "PMMVY", "documents_required": ["aadhaar", "bank_passbook", "mcp_card"]})

    # 5) Project-linked complaint (PWD)
    mk(kind="grievance", department_id="pwd", category="road_defect",
       title="Open trench left unbarricaded on North Mada Street",
       description="Road works abandoned; trench is a hazard.",
       project_id="PRJ-TN-CHN-2024-0312", priority="high",
       extra={"projectName": "Reconstruction of North Mada Street, Mylapore"})

    # ---- Second citizen: Ramesh, Lucknow (UP) — busier officer queue ----
    cid2 = store.get_or_create_citizen("9000000002")
    store.set_citizen_state(cid2, "UP", "hi-IN")
    rsvc.create_record(citizen_id=cid2, msisdn="9000000002", state_code="UP",
                       kind="grievance",
                       department_id="ration", category="missing_allocation",
                       title="Did not receive this month's ration (UP)",
                       description="Fair price shop refused allocation.",
                       channel="twilio_wa", lang="hi-IN", priority="high")

    stats = records_store.stats()
    print("\n  Seeded demo data:")
    print(f"   • primary citizen 9000000001 (Meena, TN): 5 records "
          f"(leak/ASSIGNED, no_supply/ESCALATED, garbage/RESOLVED, PMMVY app, road defect)")
    print(f"   • second citizen 9000000002 (Ramesh, UP): 1 ration complaint")
    print(f"   • totals: {stats['total']} records, byStatus={stats['byStatus']}")
    print("\n  Demo logins:  9000000001  (TN)   ·   9000000002  (UP)")


def main():
    if "--reset" in sys.argv:
        print("Resetting demo data...")
        _reset()
    print(f"Seeding demo data into {DATA} ...")
    # Loaders must run so SLA policies/schemes/projects are available.
    from backend.records import sla as _sla
    from backend import schemes as _schemes, projects as _projects
    _sla.load(); _schemes.load(); _projects.load()
    asyncio.run(_seed())
    print("\n✅ Demo data ready. Start the server, then run: python preflight_check.py\n")


if __name__ == "__main__":
    main()
