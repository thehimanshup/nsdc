"""Substrate Supervisor — a persona-routing + quality-gate layer that sits
above the three existing substrate personas (mentor, officer_copilot,
content_qa).

Today each persona is called directly by the client via `agent_id` on
`/api/v1/substrate/query` — nothing classifies whether the *right* persona
was picked, and nothing watches the judge's live groundedness numbers when
deciding whether an answer is safe to show as-is.

This module adds three things, deliberately built on top of existing
plumbing rather than inventing new storage:

  1. classify()      — cheap intent classification: which persona should
                        actually handle this message, independent of which
                        one the client asked for.
  2. quality_gate()   — reads backend.substrate.judge.stats() and, if the
                        rolling hallucination rate is above a threshold,
                        attaches a `governance_flag` to the response instead
                        of silently serving a possibly-ungrounded answer.
  3. maybe_escalate() — if the citation/evidence gate blocked the answer, or
                        the query was classified as officer-track (scheme
                        admin action rather than Q&A), push a draft note
                        into the existing officer_copilot draft-notes queue
                        (backend.substrate.drafts) instead of returning a
                        dead end to the learner.

Wired into FastAPI as a single new route:
  POST /api/v1/substrate/supervisor/query   (backend/routes_substrate.py)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .authn import Principal
from .schemas import Purpose, Role

log = logging.getLogger("substrate.supervisor")

# ---------------------------------------------------------------------------
# 1. Persona classification
# ---------------------------------------------------------------------------

# Keep this a plain regex classifier, not an LLM call — it needs to be cheap,
# deterministic, and auditable (RFP governance posture: every routing
# decision should be explainable without another model hop).
_OFFICER_ACTION_HINTS = re.compile(
    r"\b(apply|enroll|submit|file|register)\b.*\b(on my behalf|for me|apply it)\b"
    r"|\bapprove\b|\bdraft\s+(a\s+)?note\b|\bjurisdiction\b",
    re.I,
)

_CONTENTQA_HINTS = re.compile(
    r"\b(qp|qualification pack|nos|bloom|item bank|coverage|assessment item"
    r"|question paper)\b",
    re.I,
)

PERSONA_MENTOR = "mentor"
PERSONA_OFFICER = "officer_copilot"
PERSONA_CONTENTQA = "content_qa"

# Which personas a role is even allowed to be routed to (mirrors
# AGENT_ALLOWED_ROLES in routes_substrate.py — kept in sync deliberately;
# the supervisor must never route a citizen into an officer-only persona).
ROLE_CAPABLE_PERSONAS: dict[Role, set[str]] = {
    Role.learner: {PERSONA_MENTOR},
    Role.officer: {PERSONA_MENTOR, PERSONA_OFFICER},
    Role.sme: {PERSONA_MENTOR, PERSONA_CONTENTQA},
    Role.admin: {PERSONA_MENTOR, PERSONA_OFFICER, PERSONA_CONTENTQA},
}


@dataclass
class RoutingDecision:
    persona: str
    reason: str
    requested_persona: Optional[str] = None
    overridden: bool = False


def classify(question: str, role: Role, requested_persona: Optional[str] = None) -> RoutingDecision:
    """Decide which persona should actually handle this question.

    `requested_persona` is what the client asked for (if anything). The
    supervisor only overrides it when the *content* of the question clearly
    points elsewhere AND the role is capable of using that persona —
    otherwise it defers to the caller's choice.
    """
    capable = ROLE_CAPABLE_PERSONAS.get(role, {PERSONA_MENTOR})

    if _CONTENTQA_HINTS.search(question) and PERSONA_CONTENTQA in capable:
        target = PERSONA_CONTENTQA
        reason = "question references QP/NOS/item-bank content — routed to Content QA"
    elif _OFFICER_ACTION_HINTS.search(question) and PERSONA_OFFICER in capable:
        target = PERSONA_OFFICER
        reason = "question requests an administrative action, not guidance — routed to Officer Copilot"
    else:
        target = requested_persona if requested_persona in capable else PERSONA_MENTOR
        reason = "no cross-persona signal — using requested/default persona"

    overridden = bool(requested_persona) and requested_persona != target
    return RoutingDecision(persona=target, reason=reason,
                            requested_persona=requested_persona, overridden=overridden)


# ---------------------------------------------------------------------------
# 2. Live quality gate (reads the groundedness judge's rolling stats)
# ---------------------------------------------------------------------------

DEFAULT_HALLUCINATION_THRESHOLD_PCT = float(
    os.getenv("SUPERVISOR_HALLUCINATION_THRESHOLD_PCT", "20")
)
# Minimum number of judge-scored interactions before the rate is treated as
# statistically meaningful enough to raise a user-facing governance flag.
# Below this, the gate reports "insufficient sample" internally (logged)
# rather than flagging — a 55% rate off n=3 is noise, not signal, and
# waving caution banners based on it erodes trust in the real ones.
DEFAULT_MIN_SCORED_SAMPLE = int(os.getenv("SUPERVISOR_MIN_SCORED_SAMPLE", "10"))


def quality_gate(data_dir: str = "data",
                  threshold_pct: float = DEFAULT_HALLUCINATION_THRESHOLD_PCT,
                  min_scored: int = DEFAULT_MIN_SCORED_SAMPLE) -> Optional[dict]:
    """Return a governance flag dict if the rolling hallucination rate is
    above threshold AND backed by at least `min_scored` scored interactions,
    else None. Never raises — a stats-read failure should not block a
    citizen's answer, so it's logged and treated as "no flag".
    """
    try:
        from .judge import stats
        s = stats(data_dir)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("quality_gate: could not read judge stats: %s", e)
        return None

    rate = s.get("hallucination_rate_pct")
    if rate is None:
        return None
    scored = s.get("scored") or 0
    if scored < min_scored:
        log.info("quality_gate: rate %.1f%% above threshold but sample too "
                 "small to flag (scored=%d < min=%d)", rate, scored, min_scored)
        return None
    if rate > threshold_pct:
        return {
            "level": "caution",
            "message": (
                f"Recent groundedness monitoring shows a {rate:.1f}% "
                f"hallucination rate (threshold {threshold_pct:.0f}%). "
                "Treat this answer as provisional and verify citations."
            ),
            "hallucination_rate_pct": rate,
            "threshold_pct": threshold_pct,
            "scored": scored,
        }
    return None


# ---------------------------------------------------------------------------
# 3. Escalation — a small dedicated queue for cases the supervisor decides a
#    human should see: a blocked/refused answer, or a query that needed
#    officer-track handling. Deliberately NOT reusing
#    backend.substrate.drafts.generate_note(), which has a fixed purpose
#    (compiling an officer's own analytics into a status note) and would be
#    misused if repurposed to carry arbitrary escalation text.
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()


def _escalations_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "supervisor_escalations.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EscalationResult:
    escalated: bool
    escalation_id: Optional[str] = None
    note: Optional[str] = None


def maybe_escalate(principal: Principal, question: str, routing: RoutingDecision,
                    refusal_reason: Optional[str], data_dir: str = "data") -> EscalationResult:
    """If the answer was blocked (citation/evidence gate) or routing decided
    this needs an officer, append an escalation record so a human sees it —
    instead of the citizen just hitting a refusal wall with nothing done
    about it.
    """
    should_escalate = bool(refusal_reason) or routing.persona == PERSONA_OFFICER
    if not should_escalate:
        return EscalationResult(escalated=False)

    record = {
        "escalation_id": "esc_" + uuid.uuid4().hex[:10],
        "created_at": _now(),
        "actor": principal.sub,
        "role": principal.role.value if hasattr(principal.role, "value") else str(principal.role),
        "question": question,
        "routed_persona": routing.persona,
        "routing_reason": routing.reason,
        "refusal_reason": refusal_reason,
        "status": "open",
    }
    try:
        path = _escalations_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        return EscalationResult(escalated=True, escalation_id=record["escalation_id"],
                                 note="Appended to supervisor escalation queue")
    except Exception as e:  # pragma: no cover - defensive
        log.warning("maybe_escalate: could not persist escalation: %s", e)
        return EscalationResult(escalated=False, note=f"escalation failed: {e}")


def list_escalations(data_dir: str | Path = "data", status: Optional[str] = None) -> list[dict]:
    """Read back the escalation queue (for an officer/admin-facing view)."""
    path = _escalations_path(data_dir)
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status is None or rec.get("status") == status:
                out.append(rec)
    return out
