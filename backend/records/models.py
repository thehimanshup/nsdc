"""Record model + lifecycle FSM definitions — Phase 6e.

A `Record` is the unified, trackable government-casework object. The `kind`
field discriminates between the four specialisations:

    grievance          — complaints / demands / info-requests (Jansunwai-style)
    scheme_application — an application to a welfare scheme (PMAY, PM-KISAN, …)
    project_query      — a citizen following a development project (read-mostly)
    service_request    — operational requests (water tanker, new connection, …)

We use plain dataclasses with ISO-8601 string timestamps so the whole thing
serialises to JSON trivially (matching the Phase-1 store.py philosophy:
"keep it dead simple, swap to Postgres in Phase 7 without changing the
interface").
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal, Optional


RecordKind = Literal[
    "grievance", "scheme_application", "project_query", "service_request"
]


# ---------------------------------------------------------------------------
# Lifecycle FSM
# ---------------------------------------------------------------------------
#
# Generic superset of statuses. Each kind uses the relevant subset.
#
#   DRAFT → REGISTERED → ASSIGNED → IN_PROGRESS → PENDING_CITIZEN ┐
#                                       │                         │
#                                       ▼                         ▼
#                                   RESOLVED ──► FEEDBACK ──► CLOSED
#                                       │            │
#                                 (SLA breach)  (dissatisfied → reopen)
#                                       ▼            │
#                                   ESCALATED ───────┘
#   any state → REJECTED / WITHDRAWN
#
STATUS = {
    "DRAFT", "REGISTERED", "ASSIGNED", "IN_PROGRESS", "PENDING_CITIZEN",
    "ESCALATED", "RESOLVED", "FEEDBACK", "CLOSED", "REJECTED", "WITHDRAWN",
    # scheme-application subset
    "SUBMITTED", "UNDER_VERIFICATION", "SANCTIONED", "DISBURSED",
    # project subset
    "SUBSCRIBED",
}

OPEN_STATUSES = {
    "REGISTERED", "ASSIGNED", "IN_PROGRESS", "PENDING_CITIZEN", "ESCALATED",
    "SUBMITTED", "UNDER_VERIFICATION", "SANCTIONED",
}
TERMINAL_STATUSES = {"CLOSED", "REJECTED", "WITHDRAWN", "DISBURSED"}

# Statuses whose SLA clock the escalation sweeper watches.
SLA_TRACKED_STATUSES = {"ASSIGNED", "IN_PROGRESS", "ESCALATED",
                        "SUBMITTED", "UNDER_VERIFICATION"}


@dataclass
class TimelineEvent:
    """One transition in a record's life — what the citizen sees as 'history'
    and what the auditor sees as proof. Threads into the audit log via
    `audit_event_id`."""
    at: str                       # ISO-8601 UTC
    actor: str                    # citizen id, desk id, or "system"
    action: str                   # e.g. "registered", "escalated", "resolved"
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    level: int = 1
    note: str = ""
    system: bool = False
    audit_event_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Record:
    record_id: str
    kind: RecordKind
    citizen_id: str
    msisdn_hash: str              # for public "track by reference + mobile" lookup
    state_code: str
    department_id: str
    category: str
    title: str
    description: str = ""
    subcategory: Optional[str] = None
    district: Optional[str] = None
    ward_block: Optional[str] = None
    channel: str = "simulator"
    lang: str = "en-IN"
    # lifecycle
    status: str = "REGISTERED"
    current_level: int = 1
    owner_desk_id: str = ""
    priority: str = "normal"      # normal | high | emergency
    # SLA
    created_at: str = ""
    updated_at: str = ""
    sla_due_at: Optional[str] = None
    sla_policy_id: Optional[str] = None
    sla_breached: bool = False
    # linkage
    workflow_id: Optional[str] = None
    parent_record_id: Optional[str] = None
    scheme_id: Optional[str] = None         # for scheme_application
    project_id: Optional[str] = None        # for project_query / report_issue
    attachments: list[str] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    # outcome
    resolution: Optional[str] = None
    satisfaction: Optional[int] = None      # 1..5
    timeline: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        safe = {k: v for k, v in d.items() if k in known}
        return cls(**safe)

    def add_event(self, ev: TimelineEvent) -> None:
        self.timeline.append(ev.to_dict())
        self.updated_at = ev.at

    def public_view(self) -> dict:
        """Sanitised view for the no-login public tracker (Jansunwai/NCH)."""
        return {
            "recordId": self.record_id,
            "kind": self.kind,
            "department": self.department_id,
            "category": self.category,
            "title": self.title,
            "status": self.status,
            "level": self.current_level,
            "priority": self.priority,
            "stateCode": self.state_code,
            "district": self.district,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "slaDueAt": self.sla_due_at,
            "slaBreached": self.sla_breached,
            "resolution": self.resolution,
            "satisfaction": self.satisfaction,
            "timeline": [
                {k: ev.get(k) for k in
                 ("at", "action", "from_status", "to_status", "level", "note")}
                for ev in self.timeline
            ],
        }


# ---------------------------------------------------------------------------
# Human-friendly record id minting — "GRV-TN-2026-004217"
# ---------------------------------------------------------------------------

_PREFIX = {
    "grievance": "GRV",
    "scheme_application": "APP",
    "project_query": "PRJQ",
    "service_request": "SRV",
}


def mint_record_id(kind: RecordKind, state_code: str, seq: int) -> str:
    year = datetime.utcnow().year
    pref = _PREFIX.get(kind, "REC")
    sc = (state_code or "IN").upper()[:2]
    return f"{pref}-{sc}-{year}-{seq:06d}"
