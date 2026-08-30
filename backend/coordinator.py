"""Cross-Agent Coordinator.

When a citizen's query touches multiple departments (e.g. "my farm was
flooded" needs Revenue + Agriculture + CMO), the orchestrator opens a
`CoordinatorSession` here. The session is an FSM that walks through a
sequence of agent steps, accumulating context as each agent contributes.

The citizen sees a single conversation — the coordinator narrates each
handoff with a system message and renders a small progress strip in the
simulator UI.

FSM states (generic):
  DRAFT → DOCS_FETCHED → APPLICATIONS_FILED → AWAITING_REVIEW
       → APPROVED  | REJECTED

Specific flows are defined as recipes (a list of typed steps) in
COORDINATOR_RECIPES below. Phase 5 ships one: flood_relief.

Adding a new flow = add a new recipe. No orchestrator changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import settings

log = logging.getLogger("coordinator")


# ---------------------------------------------------------------------------
# Recipe — a sequence of steps describing a multi-agent flow
# ---------------------------------------------------------------------------

@dataclass
class StepSpec:
    """One step in a coordinator recipe."""
    state: str             # FSM state this step advances to
    agent_id: str          # Which department agent runs the step
    purpose: str           # Short description shown in the progress UI
    inject_context: str    # Extra context appended to the agent's prompt
    tool_id: Optional[str] = None   # Optional tool the agent is expected to call
    consent_scope: Optional[str] = None


@dataclass
class Recipe:
    id: str
    title: str
    description: str
    triggers: list[str]    # Regex patterns that detect this flow in user text
    steps: list[StepSpec]


COORDINATOR_RECIPES: dict[str, Recipe] = {}


def _register(r: Recipe) -> None:
    COORDINATOR_RECIPES[r.id] = r


# --- Flood-damage relief flow (the canonical demo) -------------------------

_register(Recipe(
    id="flood_relief",
    title="Flood-damage relief",
    description="Cross-department flow: Revenue verifies Patta → Agriculture computes crop-loss → CMO submits to relief fund.",
    triggers=[
        # English flood / inundation (case-insensitive, suffix-aware)
        r"flood|inundat|submerg",
        # Hindi flood (no \b — Devanagari \b is unreliable in Python regex)
        r"बाढ़|बारिश\s*से\s*नुकसान",
        # Tamil flood
        r"வெள்ள",
        # Crop / farm damage in English + Tamil + Hindi
        r"crop.{0,15}damag|farm.{0,15}damag",
        r"நிலம்.{0,12}சேத|பயிர்.{0,12}சேத",
        r"फसल.{0,12}नुकसान|खेत.{0,12}नुकसान|फसल.{0,12}नष्ट",
        # Cyclone, drought, disaster relief
        r"disaster|relief.{0,12}fund|cyclone|drought|cyclonic|दुर्भीक्ष|सूखा|चक्रवात",
    ],
    steps=[
        StepSpec(
            state="DOCS_FETCHED",
            agent_id="revenue",
            purpose="Verify your Patta and assess damage",
            inject_context=(
                "STEP 1 of FLOOD-RELIEF coordinator. Confirm the citizen's land details by "
                "fetching their Patta from DigiLocker. Ask for survey number if not provided. "
                "Once Patta is fetched, summarise extent and land type, and tell the citizen "
                "we'll now compute crop-loss compensation."
            ),
            tool_id="digilocker.fetch_patta",
            consent_scope="PATTA_FETCH",
        ),
        StepSpec(
            state="APPLICATIONS_FILED",
            agent_id="agriculture",
            purpose="Compute crop-loss compensation",
            inject_context=(
                "STEP 2 of FLOOD-RELIEF coordinator. The previous step (Revenue) fetched the "
                "citizen's Patta. Now compute estimated crop-loss compensation under PMFBY "
                "norms (over 33% loss qualifies) given the extent of land reported. "
                "Provide an indicative amount and explain the application steps. Mention "
                "we'll forward to CMO for final relief-fund submission."
            ),
        ),
        StepSpec(
            state="AWAITING_REVIEW",
            agent_id="cmo",
            purpose="Submit to CM's emergency relief fund",
            inject_context=(
                "STEP 3 (final) of FLOOD-RELIEF coordinator. Revenue and Agriculture have done "
                "their parts. As the Chief Minister's Cell, register a flood-relief grievance "
                "using the cmo.create_grievance tool, summarise everything that was filed, and "
                "give the citizen the grievance ID + 30-day response timeline. Reassure them "
                "in a warm tone."
            ),
            tool_id="cmo.create_grievance",
        ),
    ],
))

# --- More recipes can be added here (e.g. lost-ration-card, death-cert) ----


def match_recipe(text: str) -> Optional[Recipe]:
    """Pattern-match the citizen's text against recipe triggers."""
    t = (text or "").lower()
    for r in COORDINATOR_RECIPES.values():
        for pat in r.triggers:
            if re.search(pat, t, flags=re.UNICODE):
                return r
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class CoordinatorSession:
    session_id: str
    citizen_id: str
    recipe_id: str
    started_at: datetime
    current_step_idx: int = 0
    state: str = "DRAFT"
    completed: bool = False
    shared_context: dict = field(default_factory=dict)   # carries Patta, computed amounts, etc.
    history: list[dict] = field(default_factory=list)    # audit trail of step transitions

    @property
    def recipe(self) -> Recipe:
        return COORDINATOR_RECIPES[self.recipe_id]

    @property
    def current_step(self) -> Optional[StepSpec]:
        if 0 <= self.current_step_idx < len(self.recipe.steps):
            return self.recipe.steps[self.current_step_idx]
        return None

    def progress(self) -> dict:
        total = len(self.recipe.steps)
        idx = min(self.current_step_idx, total - 1) if not self.completed else total
        return {
            "sessionId": self.session_id,
            "recipeId": self.recipe_id,
            "title": self.recipe.title,
            "currentIdx": idx,
            "total": total,
            "state": self.state,
            "completed": self.completed,
            "steps": [
                {
                    "agentId": s.agent_id, "purpose": s.purpose,
                    "state": s.state,
                    "done": i < self.current_step_idx or self.completed,
                    "current": (i == self.current_step_idx and not self.completed),
                }
                for i, s in enumerate(self.recipe.steps)
            ],
        }


_SESSIONS: dict[str, CoordinatorSession] = {}            # session_id -> session
_BY_CITIZEN: dict[str, str] = {}                          # citizen_id -> session_id


def open_session(citizen_id: str, recipe_id: str) -> CoordinatorSession:
    """Create a new coordinator session for a citizen."""
    sid = f"coord_{uuid.uuid4().hex[:12]}"
    s = CoordinatorSession(
        session_id=sid, citizen_id=citizen_id,
        recipe_id=recipe_id, started_at=datetime.utcnow(),
    )
    _SESSIONS[sid] = s
    _BY_CITIZEN[citizen_id] = sid
    log.info("Opened coordinator session %s for citizen=%s recipe=%s",
             sid, citizen_id, recipe_id)
    return s


def get_active(citizen_id: str) -> Optional[CoordinatorSession]:
    sid = _BY_CITIZEN.get(citizen_id)
    if not sid:
        return None
    s = _SESSIONS.get(sid)
    if s and not s.completed:
        return s
    return None


def get_session(session_id: str) -> Optional[CoordinatorSession]:
    return _SESSIONS.get(session_id)


def advance(session_id: str, *, contribution: dict = None) -> Optional[CoordinatorSession]:
    """Mark current step done and advance to the next."""
    s = _SESSIONS.get(session_id)
    if not s:
        return None
    step = s.current_step
    if step is None:
        return s
    s.history.append({
        "stepIdx": s.current_step_idx, "agentId": step.agent_id,
        "state_before": s.state, "state_after": step.state,
        "completed_at": datetime.utcnow().isoformat(),
        "contribution": contribution or {},
    })
    if contribution:
        s.shared_context.update(contribution)
    s.state = step.state
    s.current_step_idx += 1
    if s.current_step_idx >= len(s.recipe.steps):
        s.completed = True
        _BY_CITIZEN.pop(s.citizen_id, None)
    log.info("Coordinator %s advanced to step=%d state=%s completed=%s",
             session_id, s.current_step_idx, s.state, s.completed)
    return s


def close_session(citizen_id: str) -> Optional[CoordinatorSession]:
    sid = _BY_CITIZEN.pop(citizen_id, None)
    if not sid:
        return None
    s = _SESSIONS.get(sid)
    if s:
        s.completed = True
    return s


def list_recipes() -> list[dict]:
    return [
        {"id": r.id, "title": r.title, "description": r.description,
         "agentIds": [s.agent_id for s in r.steps],
         "steps": len(r.steps)}
        for r in COORDINATOR_RECIPES.values()
    ]
