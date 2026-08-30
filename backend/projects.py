"""Development-project catalog — Phase 6e.

Loads data/projects.json (roads, buildings, water works). Citizens can find
projects near them, track milestones/% complete, and report an issue (which
creates a linked grievance record via records.service).

All projects are synthetic and flagged is_mock — project *metadata* (cost,
contractor, milestones) is public-domain in form; no live PII is fetched.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("projects")

_PROJECTS: dict[str, dict] = {}


def _data_path() -> Path:
    p = Path(settings.data_dir) / "projects.json"
    if not p.exists():
        alt = Path(__file__).resolve().parent.parent / "data" / "projects.json"
        if alt.exists():
            return alt
    return p


def load() -> int:
    global _PROJECTS
    try:
        loaded = json.loads(_data_path().read_text(encoding="utf-8"))
        if settings.is_production:
            loaded = {k: v for k, v in loaded.items() if not v.get("is_mock", False)}
        _PROJECTS = loaded
    except Exception as e:
        log.warning("projects load failed: %s", e)
        _PROJECTS = {}
    log.info("Loaded %d development projects", len(_PROJECTS))
    return len(_PROJECTS)


def get(project_id: str) -> Optional[dict]:
    return _PROJECTS.get((project_id or "").strip())


def all_projects() -> list[dict]:
    return list(_PROJECTS.values())


def find(*, state_code: str = "", district: str = "", ptype: str = "",
         query: str = "", limit: int = 20) -> list[dict]:
    q = (query or "").lower()
    out = []
    for p in _PROJECTS.values():
        if state_code and p.get("state_code", "").upper() != state_code.upper():
            continue
        if district and district.lower() not in p.get("district", "").lower():
            continue
        if ptype and p.get("type") != ptype:
            continue
        if q:
            text = f"{p.get('name','')} {p.get('district','')} {p.get('ward_block','')} {p.get('type','')}".lower()
            if q not in text and not any(t in text for t in q.split() if len(t) > 2):
                continue
        out.append(p)
    out.sort(key=lambda p: p.get("percent_complete", 0))
    return out[:limit]


def summary(project_id: str) -> Optional[dict]:
    p = get(project_id)
    if not p:
        return None
    ms = p.get("milestones", [])
    done = sum(1 for m in ms if m.get("done"))
    return {
        "projectId": p["project_id"], "name": p["name"], "type": p["type"],
        "department": p.get("department"), "stateCode": p.get("state_code"),
        "district": p.get("district"), "wardBlock": p.get("ward_block"),
        "status": p.get("status"), "percentComplete": p.get("percent_complete"),
        "sanctionedCostLakh": p.get("sanctioned_cost_lakh"),
        "fundingSource": p.get("funding_source"),
        "contractor": p.get("contractor"),
        "startDate": p.get("start_date"),
        "expectedCompletion": p.get("expected_completion"),
        "milestonesDone": done, "milestonesTotal": len(ms),
        "milestones": ms, "isMock": p.get("is_mock", False),
    }
