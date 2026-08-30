#!/usr/bin/env python3
"""CMO accuracy regression test (Phase 6f).

Proves the fixes for the live bug where the CM-office agent *claimed* to register
grievances and handed out invented ticket numbers (e.g. "G-2024-00567") while
nothing reached the Casework Operations Console.

Checks:
  1. Intent routing — escalation/complaint/ticket/status phrasings (en/hi/ta) pick
     the right tool (records.create / records.track / records.list_mine), and a
     pension+escalate sentence no longer mis-routes to schemes.search.
  2. Real persistence — running the picked create tool actually writes a Record
     that shows up in the admin/ops queue (records_store.query) and is trackable.
  3. Anti-fabrication — _fix_fabricated_refs removes invented numbers when no real
     record exists, and swaps in the real reference when one does.
"""
import sys, re, asyncio
sys.path.insert(0, ".")

import backend.orchestrator as orch
from backend.tools import get_tool
from backend.records.store import records_store
from backend.store import store

PASS = "\033[92mPASS\033[0m"; FAIL = "\033[91mFAIL\033[0m"
fails = 0
def check(name, cond):
    global fails
    print(f"  [{PASS if cond else FAIL}] {name}")
    if not cond: fails += 1

# ---- 1. intent routing ------------------------------------------------------
print("\n[1] Intent routing for CMO")
cases = [
    ("My pension application is pending for 6 months. Can I escalate?", "records.create"),
    ("koi ticket number to do",                                          "records.create"),
    ("I want to raise a complaint about my ration card",                 "records.create"),
    ("mujhe shikayat darj karni hai",                                    "records.create"),
    ("எனக்கு புகார் பதிவு செய்ய வேண்டும்",                                 "records.create"),
    ("what is the status of GRV-TN-2026-000123",                         "records.track"),
    ("show me all my complaints",                                        "records.list_mine"),
    ("what welfare schemes are there for senior citizens",               "schemes.search"),
]
for text, expect in cases:
    tool = orch._mock_pick_tool("cmo", text)
    got = tool.id if tool else None
    check(f"{expect:18s} <- {text[:46]!r}  (got {got})", got == expect)

# ---- 2. real persistence (shows in admin/ops) -------------------------------
print("\n[2] A registered grievance actually persists + is trackable")
cid = store.get_or_create_citizen("9898989898")
before = len(records_store.query(department_id="cmo"))

async def run_create():
    tool = orch._mock_pick_tool("cmo", "My pension is pending 6 months, please escalate")
    args = orch._mock_tool_args(tool, "cmo", "My pension is pending 6 months, please escalate", "simulator")
    return await tool.execute(args, cid)

res = asyncio.run(run_create())
check("create tool returned ok + record_id", res.get("ok") and bool(res.get("record_id")))
rid = res.get("record_id", "")
check(f"reference uses official format ({rid})", bool(re.match(r"^(GRV|APP|REC|SRV)-[A-Z]{2}-\d{4}-\d+$", rid)))
after = records_store.query(department_id="cmo")
check("record now visible in admin/ops CMO queue", len(after) == before + 1)
check("category guessed as pension_delay", res.get("category") == "pension_delay")
check("escalation marked high priority", (records_store.get(rid).priority == "high"))

# trackable
async def run_track():
    t = get_tool("records.track")
    return await t.execute({"record_id": rid}, cid)
tv = asyncio.run(run_track())
_trec = tv.get("record", {})
check("track returns the real record",
      tv.get("ok") and (_trec.get("recordId") or _trec.get("record_id")) == rid)

# duplicate guard: same category within 10 min reuses the record
res2 = asyncio.run(run_create())
check("duplicate guard reuses same record (no spam)", res2.get("record_id") == rid)

# ---- 3. anti-fabrication ----------------------------------------------------
print("\n[3] Anti-fabrication guard")
fake = "हाँ, मैंने आपके लिए एक टिकट बनाया है। आपका ट्रैकिंग नंबर G-2024-00567 है।"
fixed_no_real = orch._fix_fabricated_refs(fake, cid, "", lang="hi-IN")
check("invented G-2024-00567 removed when no real record", "G-2024-00567" not in fixed_no_real)
check("honest replacement text added", ("दर्ज" in fixed_no_real or "रेफरेंस" in fixed_no_real))

fixed_real = orch._fix_fabricated_refs(fake, cid, rid, lang="hi-IN")
check("fake number swapped for the REAL reference", rid in fixed_real and "G-2024-00567" not in fixed_real)

real_ok = f"Your complaint is registered as {rid}. Track it with this reference."
check("a genuine reference is left untouched", orch._fix_fabricated_refs(real_ok, cid, rid, lang="en-IN") == real_ok)

print("\n" + "="*60)
print("RESULT:", "ALL PASS ✅" if fails == 0 else f"{fails} CHECK(S) FAILED ❌")
print("="*60)
sys.exit(1 if fails else 0)
