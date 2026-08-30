#!/usr/bin/env python3
"""All-agents accuracy regression test (Phase 6f).

Verifies the CMO-style accuracy fixes now hold for EVERY agent:
  1. Routing — a domain complaint routes to records.create (and the agent is
     allowed to use it); a scheme/info question routes to schemes.search.
  2. Real persistence — running the create tool for each department writes a
     Record that shows up in that department's admin/ops queue and is trackable.
  3. Anti-fabrication — invented reference numbers are stripped / replaced.

Run on the server machine:  python3 all_agents_accuracy_test.py
"""
import sys, re, asyncio
sys.path.insert(0, ".")

import backend.orchestrator as orch
from backend.tools import get_tool
from backend.records.store import records_store
from backend.store import store

PASS, FAIL = "PASS", "FAIL"
fails = 0
def chk(name, cond):
    global fails
    print(f"  [{PASS if cond else FAIL}] {name}")
    if not cond: fails += 1

AGENTS = ["cmo", "health", "water", "revenue", "transport", "ration",
          "agriculture", "housing", "wcd", "social", "pwd"]

# A natural domain complaint per agent (the kind that must become a record).
COMPLAINT = {
    "cmo":         "I want to escalate my pending grievance, no response for months",
    "health":      "I want to file a complaint, the PHC refused to admit my mother",
    "water":       "there is a sewage leak on my street, please register a complaint",
    "revenue":     "I want to raise a complaint, my patta transfer is stuck for months",
    "transport":   "I want to lodge a complaint about my licence renewal delay",
    "ration":      "register a complaint, my ration shop is not giving this month's rice",
    "agriculture": "I want to file a grievance, my crop insurance claim is pending",
    "housing":     "raise a complaint, my PMAY house payment has not come",
    "wcd":         "I want to file a complaint about my maternity benefit not credited",
    "social":      "escalate my old age pension, pending for 6 months",
    "pwd":         "register a complaint about an abandoned road work near me",
}

print("[1] Each agent routes a domain complaint to records.create (and is allowed)")
for a in AGENTS:
    tool = orch._mock_pick_tool(a, COMPLAINT[a])
    tid = tool.id if tool else None
    # water has its own specialised complaint tool — both are valid record creators
    ok = tid in ("records.create", "water.register_complaint", "projects.report_issue")
    chk(f"{a:12s} -> {tid}", ok)

print("\n[2] Each agent's complaint persists to admin/ops + is trackable")
for a in AGENTS:
    cid = store.get_or_create_citizen("90000000" + str(10 + AGENTS.index(a)))
    tool = orch._mock_pick_tool(a, COMPLAINT[a])
    args = orch._mock_tool_args(tool, a, COMPLAINT[a], "simulator")
    res = asyncio.run(tool.execute(args, cid))
    rid = res.get("record_id", "")
    dept = res.get("department")
    in_queue = any(r.record_id == rid for r in records_store.query(department_id=dept))
    tv = asyncio.run(get_tool("records.track").execute({"record_id": rid}, cid))
    trk = tv.get("ok") and (tv.get("record", {}).get("recordId") == rid)
    chk(f"{a:12s} created {rid} (dept={dept}) · in admin/ops={in_queue} · trackable={trk}",
        bool(rid) and in_queue and trk)

print("\n[3] Scheme info routes to schemes.search for scheme agents")
for a, q in [("social", "what pension schemes am I eligible for"),
             ("housing", "what housing schemes are there for me"),
             ("wcd", "is there a maternity benefit scheme")]:
    tool = orch._mock_pick_tool(a, q)
    chk(f"{a:12s} -> {tool.id if tool else None}", tool and tool.id == "schemes.search")

print("\n[4] Anti-fabrication holds for a non-CMO agent")
cid = store.get_or_create_citizen("9000000099")
fake = "Your ticket number is G-2024-00567."
chk("fake number stripped when no real record",
    "G-2024-00567" not in orch._fix_fabricated_refs(fake, cid, "", lang="en-IN"))

print("\n" + "=" * 60)
print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} CHECK(S) FAILED")
print("=" * 60)
sys.exit(1 if fails else 0)
