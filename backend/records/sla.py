"""SLA policies + escalation matrix (desks) — Phase 6e.

Models the MP CM Helpline L1→L4 escalation:
  - a *Desk* is one rung of a department's ladder (ward officer → zone →
    city → board MD / Principal Secretary), each with a jurisdiction + a
    human officer label.
  - an *SLA policy* lists, per category, how many hours each level gets
    before the record auto-escalates to the next desk.

Both are JSON-backed (data/desks.json, data/sla_policies.json) and seeded
from the in-code defaults below on first start — exactly the migration
pattern admin_storage.py uses for agents. Admins edit the JSON; the registry
hot-reloads.

DEMO CLOCK: set RECORDS_SLA_DEMO=true (default) to interpret policy "hours"
as *minutes* so escalation is watchable in a stakeholder demo. Set it to
false for real-world day-scale SLAs.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("records.sla")

# Phase 6e — defaults to FALSE (real day-scale SLAs). Set RECORDS_SLA_DEMO=true
# ONLY for demos, where it collapses policy "hours" into minutes so escalation
# is watchable. Shipping it on by default would escalate every real complaint
# to the Principal Secretary within minutes.
_DEMO_CLOCK = os.getenv("RECORDS_SLA_DEMO", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_DEFAULT_DESKS = {
    # Water — the canonical demo ladder (CMWSSB)
    "water.l1.ward":   {"department_id": "water", "level": 1, "officer_label": "Ward Engineer (AE)",        "jurisdiction": "ward"},
    "water.l2.zone":   {"department_id": "water", "level": 2, "officer_label": "Zonal Executive Engineer (EE)", "jurisdiction": "zone"},
    "water.l3.city":   {"department_id": "water", "level": 3, "officer_label": "Superintending Engineer (SE)", "jurisdiction": "city"},
    "water.l4.board":  {"department_id": "water", "level": 4, "officer_label": "Managing Director, Water Board", "jurisdiction": "state"},
    # Generic grievance ladder (CMO) — MP-style Patwari → … → Principal Secretary
    "cmo.l1.field":    {"department_id": "cmo", "level": 1, "officer_label": "Field Officer / Patwari",     "jurisdiction": "block"},
    "cmo.l2.taluk":    {"department_id": "cmo", "level": 2, "officer_label": "Revenue Inspector / Tahsildar", "jurisdiction": "taluk"},
    "cmo.l3.district": {"department_id": "cmo", "level": 3, "officer_label": "District Collector",          "jurisdiction": "district"},
    "cmo.l4.secy":     {"department_id": "cmo", "level": 4, "officer_label": "Principal Secretary",         "jurisdiction": "state"},
    # Revenue
    "revenue.l1.vao":  {"department_id": "revenue", "level": 1, "officer_label": "Village Admin Officer (VAO)", "jurisdiction": "village"},
    "revenue.l2.taluk":{"department_id": "revenue", "level": 2, "officer_label": "Tahsildar",               "jurisdiction": "taluk"},
    "revenue.l3.rdo":  {"department_id": "revenue", "level": 3, "officer_label": "Revenue Divisional Officer", "jurisdiction": "division"},
    "revenue.l4.collr":{"department_id": "revenue", "level": 4, "officer_label": "District Collector",      "jurisdiction": "district"},
    # Ration / Civil Supplies
    "ration.l1.fps":   {"department_id": "ration", "level": 1, "officer_label": "Fair Price Shop Inspector", "jurisdiction": "shop"},
    "ration.l2.taluk": {"department_id": "ration", "level": 2, "officer_label": "Taluk Supply Officer",     "jurisdiction": "taluk"},
    "ration.l3.dso":   {"department_id": "ration", "level": 3, "officer_label": "District Supply Officer",  "jurisdiction": "district"},
    # PWD (development projects)
    "pwd.l1.ae":       {"department_id": "pwd", "level": 1, "officer_label": "Assistant Engineer (PWD)",    "jurisdiction": "ward"},
    "pwd.l2.ee":       {"department_id": "pwd", "level": 2, "officer_label": "Executive Engineer (PWD)",    "jurisdiction": "division"},
    "pwd.l3.ce":       {"department_id": "pwd", "level": 3, "officer_label": "Chief Engineer (PWD)",        "jurisdiction": "state"},
    # Housing
    "housing.l1.desk": {"department_id": "housing", "level": 1, "officer_label": "Housing Scheme Clerk",    "jurisdiction": "block"},
    "housing.l2.bdo":  {"department_id": "housing", "level": 2, "officer_label": "Block Development Officer", "jurisdiction": "block"},
    "housing.l3.ceo":  {"department_id": "housing", "level": 3, "officer_label": "CEO, Housing Board",       "jurisdiction": "state"},
    # Women & Child
    "wcd.l1.awc":      {"department_id": "wcd", "level": 1, "officer_label": "Anganwadi Supervisor",        "jurisdiction": "block"},
    "wcd.l2.cdpo":     {"department_id": "wcd", "level": 2, "officer_label": "Child Dev Project Officer",   "jurisdiction": "project"},
    "wcd.l3.dpo":      {"department_id": "wcd", "level": 3, "officer_label": "District Programme Officer",  "jurisdiction": "district"},
    # Social Welfare (senior citizen / pensions)
    "social.l1.vao":   {"department_id": "social", "level": 1, "officer_label": "Welfare Assistant",        "jurisdiction": "village"},
    "social.l2.dswo":  {"department_id": "social", "level": 2, "officer_label": "District Social Welfare Officer", "jurisdiction": "district"},
    "social.l3.dir":   {"department_id": "social", "level": 3, "officer_label": "Director, Social Welfare", "jurisdiction": "state"},
    # Health
    "health.l1.phc":   {"department_id": "health", "level": 1, "officer_label": "PHC Medical Officer",      "jurisdiction": "phc"},
    "health.l2.dmo":   {"department_id": "health", "level": 2, "officer_label": "District Medical Officer", "jurisdiction": "district"},
    "health.l3.dir":   {"department_id": "health", "level": 3, "officer_label": "Director, Health Services","jurisdiction": "state"},
    # Agriculture
    "agriculture.l1.ado": {"department_id": "agriculture", "level": 1, "officer_label": "Agriculture Officer (ADO)", "jurisdiction": "block"},
    "agriculture.l2.jd":  {"department_id": "agriculture", "level": 2, "officer_label": "Joint Director Agriculture", "jurisdiction": "district"},
    # Transport
    "transport.l1.rto":   {"department_id": "transport", "level": 1, "officer_label": "RTO Inspector",      "jurisdiction": "rto"},
    "transport.l2.dto":   {"department_id": "transport", "level": 2, "officer_label": "Regional Transport Officer", "jurisdiction": "region"},
}

# SLA policies: per category, the ladder of (level, desk, hours).
_DEFAULT_SLA_POLICIES = {
    "water.leak": {"levels": [
        {"level": 1, "desk": "water.l1.ward", "hours": 24},
        {"level": 2, "desk": "water.l2.zone", "hours": 72},
        {"level": 3, "desk": "water.l3.city", "hours": 168},
        {"level": 4, "desk": "water.l4.board", "hours": 168},
    ]},
    "water.no_supply": {"levels": [
        {"level": 1, "desk": "water.l1.ward", "hours": 12},
        {"level": 2, "desk": "water.l2.zone", "hours": 48},
        {"level": 3, "desk": "water.l3.city", "hours": 120},
    ]},
    "water.sewer_blockage": {"levels": [
        {"level": 1, "desk": "water.l1.ward", "hours": 12},
        {"level": 2, "desk": "water.l2.zone", "hours": 48},
        {"level": 3, "desk": "water.l3.city", "hours": 120},
    ]},
    "grievance.general": {"levels": [
        {"level": 1, "desk": "cmo.l1.field", "hours": 168},      # 7 days (MP L1)
        {"level": 2, "desk": "cmo.l2.taluk", "hours": 360},      # 15 days (MP L2)
        {"level": 3, "desk": "cmo.l3.district", "hours": 168},
        {"level": 4, "desk": "cmo.l4.secy", "hours": 168},
    ]},
    "revenue.patta_correction": {"levels": [
        {"level": 1, "desk": "revenue.l1.vao", "hours": 168},
        {"level": 2, "desk": "revenue.l2.taluk", "hours": 240},
        {"level": 3, "desk": "revenue.l3.rdo", "hours": 168},
        {"level": 4, "desk": "revenue.l4.collr", "hours": 168},
    ]},
    "ration.missing_allocation": {"levels": [
        {"level": 1, "desk": "ration.l1.fps", "hours": 72},
        {"level": 2, "desk": "ration.l2.taluk", "hours": 168},
        {"level": 3, "desk": "ration.l3.dso", "hours": 168},
    ]},
    "pwd.road_defect": {"levels": [
        {"level": 1, "desk": "pwd.l1.ae", "hours": 120},
        {"level": 2, "desk": "pwd.l2.ee", "hours": 240},
        {"level": 3, "desk": "pwd.l3.ce", "hours": 240},
    ]},
    # Scheme applications use a verification ladder rather than escalation
    "scheme.application": {"levels": [
        {"level": 1, "desk": "social.l1.vao", "hours": 240},     # verification window
        {"level": 2, "desk": "social.l2.dswo", "hours": 360},
    ]},
}

# Fallback policy when a category has none defined.
_FALLBACK_POLICY_ID = "grievance.general"


@dataclass
class Desk:
    desk_id: str
    department_id: str
    level: int
    officer_label: str
    jurisdiction: str


_DESKS: dict[str, Desk] = {}
_POLICIES: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Load / seed
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    p = Path(settings.data_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _seed(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            txt = path.read_text(encoding="utf-8").strip()
            if txt:
                return json.loads(txt)
        except Exception as e:
            log.warning("Could not read %s (%s) — using defaults", path, e)
    try:
        path.write_text(json.dumps(default, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except Exception as e:
        log.warning("Could not seed %s: %s", path, e)
    return default


def load() -> None:
    """Load desks + SLA policies, seeding defaults on first start."""
    global _DESKS, _POLICIES
    desks_raw = _seed(_path("desks.json"), _DEFAULT_DESKS)
    _DESKS = {
        did: Desk(desk_id=did, department_id=d.get("department_id", ""),
                  level=int(d.get("level", 1)),
                  officer_label=d.get("officer_label", "Officer"),
                  jurisdiction=d.get("jurisdiction", ""))
        for did, d in desks_raw.items()
    }
    _POLICIES = _seed(_path("sla_policies.json"), _DEFAULT_SLA_POLICIES)
    log.info("SLA layer loaded: %d desks, %d policies (demo_clock=%s)",
             len(_DESKS), len(_POLICIES), _DEMO_CLOCK)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_desk(desk_id: str) -> Optional[Desk]:
    return _DESKS.get(desk_id)


def desk_label(desk_id: str) -> str:
    d = _DESKS.get(desk_id)
    return d.officer_label if d else (desk_id or "Officer")


def policy_for_category(department_id: str, category: str) -> tuple[str, dict]:
    """Resolve the SLA policy id + body for a (department, category).

    Tries 'department.category', then 'category', then the fallback.
    """
    for pid in (f"{department_id}.{category}", category):
        if pid in _POLICIES:
            return pid, _POLICIES[pid]
    return _FALLBACK_POLICY_ID, _POLICIES.get(_FALLBACK_POLICY_ID,
                                              _DEFAULT_SLA_POLICIES[_FALLBACK_POLICY_ID])


def _level_entry(policy: dict, level: int) -> Optional[dict]:
    for lv in policy.get("levels", []):
        if int(lv.get("level", 0)) == level:
            return lv
    return None


def hours_to_delta(hours: float) -> timedelta:
    """Demo clock collapses hours→minutes so escalation is watchable."""
    if _DEMO_CLOCK:
        return timedelta(minutes=hours)
    return timedelta(hours=hours)


def first_desk_and_due(policy_id: str) -> tuple[str, str, int]:
    """Return (desk_id, sla_due_at_iso, level) for level 1 of a policy."""
    policy = _POLICIES.get(policy_id, {})
    lv = _level_entry(policy, 1) or {"desk": "cmo.l1.field", "hours": 168, "level": 1}
    due = datetime.utcnow() + hours_to_delta(float(lv.get("hours", 168)))
    return lv.get("desk", "cmo.l1.field"), due.isoformat(), 1


def next_level(policy_id: str, current_level: int) -> Optional[tuple[str, str, int]]:
    """Return (desk_id, sla_due_at_iso, level) for current_level+1, or None
    if there is no higher level (top of the ladder reached)."""
    policy = _POLICIES.get(policy_id, {})
    nl = current_level + 1
    lv = _level_entry(policy, nl)
    if not lv:
        return None
    due = datetime.utcnow() + hours_to_delta(float(lv.get("hours", 168)))
    return lv.get("desk", ""), due.isoformat(), nl


def all_desks_json() -> dict:
    return {d.desk_id: {"department_id": d.department_id, "level": d.level,
                        "officer_label": d.officer_label,
                        "jurisdiction": d.jurisdiction}
            for d in _DESKS.values()}


def all_policies_json() -> dict:
    return dict(_POLICIES)


def demo_clock() -> bool:
    return _DEMO_CLOCK
