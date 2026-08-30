"""Workflow templates — Phase 6e.

Generalises the Phase-5 hard-coded coordinator recipes into admin-editable
data (data/workflows.json). Two shapes:

  - cross_agent  : a multi-department FSM → registered into coordinator's
                   COORDINATOR_RECIPES (e.g. flood_relief).
  - single_agent : a one-department slot-fill → records.create flow, matched
                   by trigger keywords and surfaced to the orchestrator.

On load we (a) register cross_agent templates as coordinator Recipes (so the
existing orchestrator coordinator path drives them unchanged) and (b) keep
single_agent templates available for trigger-matching + admin display.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("workflows")

_TEMPLATES: dict[str, dict] = {}


def _data_path() -> Path:
    p = Path(settings.data_dir) / "workflows.json"
    if not p.exists():
        alt = Path(__file__).resolve().parent.parent / "data" / "workflows.json"
        if alt.exists():
            return alt
    return p


def load() -> int:
    """Load templates + register cross_agent ones into the coordinator."""
    global _TEMPLATES
    try:
        _TEMPLATES = json.loads(_data_path().read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("workflows load failed: %s", e)
        _TEMPLATES = {}

    # Register cross_agent templates as coordinator recipes (idempotent).
    try:
        from .coordinator import Recipe, StepSpec, COORDINATOR_RECIPES
        for wid, t in _TEMPLATES.items():
            if t.get("shape") != "cross_agent":
                continue
            steps = [
                StepSpec(
                    state=s.get("state", "STEP"),
                    agent_id=s.get("agent_id", "cmo"),
                    purpose=s.get("purpose", ""),
                    inject_context=s.get("inject_context", ""),
                    tool_id=s.get("tool_id"),
                    consent_scope=s.get("consent_scope"),
                )
                for s in t.get("steps", [])
            ]
            COORDINATOR_RECIPES[wid] = Recipe(
                id=wid, title=t.get("title", wid),
                description=t.get("description", ""),
                triggers=t.get("triggers", []), steps=steps,
            )
        log.info("Loaded %d workflow templates (%d cross-agent registered)",
                 len(_TEMPLATES),
                 sum(1 for t in _TEMPLATES.values() if t.get("shape") == "cross_agent"))
    except Exception as e:
        log.warning("coordinator registration skipped: %s", e)
    return len(_TEMPLATES)


def get(workflow_id: str) -> Optional[dict]:
    return _TEMPLATES.get(workflow_id)


def all_templates() -> list[dict]:
    return list(_TEMPLATES.values())


def match_single_agent(text: str, department_id: str = "") -> Optional[dict]:
    """Find a single_agent workflow whose triggers match the text."""
    t = (text or "").lower()
    for tpl in _TEMPLATES.values():
        if tpl.get("shape") != "single_agent":
            continue
        if department_id and tpl.get("owner_department") != department_id:
            continue
        for pat in tpl.get("triggers", []):
            try:
                if re.search(pat, t, flags=re.UNICODE | re.IGNORECASE):
                    return tpl
            except re.error:
                if pat.lower() in t:
                    return tpl
    return None
