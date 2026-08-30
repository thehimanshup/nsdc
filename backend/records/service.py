"""Record service — high-level casework operations — Phase 6e.

This is the only module the rest of the backend should call. It composes:
  - records.store      (persistence)
  - records.sla        (escalation matrix + SLA timers)
  - backend.audit      (every transition is an audit event — DPDP traceability)
  - a notifier hook    (WS / WhatsApp push, wired by the orchestrator)

Lifecycle operations: create → transition → escalate → resolve → feedback →
close / reopen, plus citizen self-service (note, reminder, withdraw, track).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from .models import (Record, TimelineEvent, OPEN_STATUSES, TERMINAL_STATUSES)
from .store import records_store, msisdn_hash
from . import sla

log = logging.getLogger("records.service")


# ---------------------------------------------------------------------------
# Notifier hook — the orchestrator registers a coroutine that pushes a
# `record_update` WS frame (and optionally a WhatsApp message) to the citizen.
# Kept as a hook so records/ has no dependency on ws_manager / channels.
# ---------------------------------------------------------------------------

_notifier: Optional[Callable] = None


def set_notifier(fn: Callable) -> None:
    global _notifier
    _notifier = fn


async def _notify(record: Record, event: str, message: str) -> None:
    if _notifier is None:
        return
    try:
        await _notifier(record, event, message)
    except Exception as e:
        log.warning("notifier failed for %s: %s", record.record_id, e)


def _audit(actor: str, action: str, record: Record, payload: dict) -> Optional[str]:
    try:
        from .. import audit as _a
        ev = _a.append_event(
            actor=actor, action=action,
            resource={"recordId": record.record_id, "kind": record.kind,
                      "department": record.department_id},
            payload=payload,
        )
        # audit.append_event returns the event (or dict); be defensive
        if isinstance(ev, dict):
            return ev.get("id") or ev.get("event_id")
        return getattr(ev, "id", None) or getattr(ev, "event_id", None)
    except Exception as e:
        log.debug("audit append skipped: %s", e)
        return None


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_record(
    *,
    kind: str,
    citizen_id: str,
    msisdn: str,
    state_code: str,
    department_id: str,
    category: str,
    title: str,
    description: str = "",
    subcategory: Optional[str] = None,
    district: Optional[str] = None,
    ward_block: Optional[str] = None,
    channel: str = "simulator",
    lang: str = "en-IN",
    priority: str = "normal",
    workflow_id: Optional[str] = None,
    parent_record_id: Optional[str] = None,
    scheme_id: Optional[str] = None,
    project_id: Optional[str] = None,
    attachments: Optional[list] = None,
    documents: Optional[list] = None,
    initial_status: str = "REGISTERED",
    extra: Optional[dict] = None,
) -> Record:
    """Create + persist a record, assign the L1 desk, start the SLA clock."""
    policy_id, _ = sla.policy_for_category(department_id, category)
    desk_id, due_at, level = sla.first_desk_and_due(policy_id)

    rid = records_store.new_id(kind, state_code)
    now = _now()
    rec = Record(
        record_id=rid, kind=kind, citizen_id=citizen_id,
        msisdn_hash=msisdn_hash(msisdn), state_code=(state_code or "IN").upper(),
        department_id=department_id, category=category, subcategory=subcategory,
        title=title[:160] or category, description=description[:2000],
        district=district, ward_block=ward_block, channel=channel, lang=lang,
        status=initial_status, current_level=level, owner_desk_id=desk_id,
        priority=priority, created_at=now, updated_at=now,
        sla_due_at=due_at, sla_policy_id=policy_id,
        workflow_id=workflow_id, parent_record_id=parent_record_id,
        scheme_id=scheme_id, project_id=project_id,
        attachments=list(attachments or []), documents=list(documents or []),
        extra=extra or {},
    )
    aid = _audit(citizen_id, "record.create", rec,
                 {"category": category, "channel": channel, "priority": priority})
    rec.add_event(TimelineEvent(
        at=now, actor=citizen_id, action="registered",
        from_status="DRAFT", to_status=initial_status, level=level,
        note=f"Registered with {sla.desk_label(desk_id)}.", audit_event_id=aid,
    ))
    # immediately mark ASSIGNED to the L1 desk (matches real intake)
    if initial_status == "REGISTERED":
        rec.status = "ASSIGNED"
        rec.add_event(TimelineEvent(
            at=now, actor="system", action="assigned",
            from_status="REGISTERED", to_status="ASSIGNED", level=level,
            note=f"Auto-assigned to {sla.desk_label(desk_id)} ({desk_id}).",
            system=True,
        ))
    records_store.add(rec)
    log.info("Created %s (%s/%s) → desk=%s due=%s", rid, kind, category, desk_id, due_at)
    return rec


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

async def transition(record: Record, *, to_status: str, actor: str,
                     action: str, note: str = "", resolution: str = "",
                     notify: bool = True) -> Record:
    frm = record.status
    record.status = to_status
    if resolution:
        record.resolution = resolution
    aid = _audit(actor, f"record.{action}", record,
                 {"from": frm, "to": to_status})
    record.add_event(TimelineEvent(
        at=_now(), actor=actor, action=action, from_status=frm,
        to_status=to_status, level=record.current_level, note=note,
        system=(actor == "system"), audit_event_id=aid,
    ))
    records_store.save(record)
    if notify:
        await _notify(record, action,
                      note or f"Your {record.kind.replace('_', ' ')} "
                              f"{record.record_id} is now {to_status}.")
    return record


async def escalate(record: Record, *, reason: str = "SLA breached") -> Record:
    """Move to the next desk in the escalation matrix (MP L1→L4 model)."""
    nxt = sla.next_level(record.sla_policy_id or "", record.current_level)
    record.sla_breached = True
    if not nxt:
        # Top of the ladder — flag breach, keep at the highest desk.
        aid = _audit("system", "record.escalation_capped", record,
                     {"level": record.current_level, "reason": reason})
        record.add_event(TimelineEvent(
            at=_now(), actor="system", action="escalation_capped",
            from_status=record.status, to_status=record.status,
            level=record.current_level, system=True,
            note=f"{reason}. Already at top desk "
                 f"({sla.desk_label(record.owner_desk_id)}); flagged for senior review.",
            audit_event_id=aid))
        records_store.save(record)
        await _notify(record, "escalation_capped",
                      f"{record.record_id} flagged for senior review "
                      f"({sla.desk_label(record.owner_desk_id)}).")
        return record

    desk_id, due_at, level = nxt
    frm_level, frm_desk = record.current_level, record.owner_desk_id
    record.current_level = level
    record.owner_desk_id = desk_id
    record.sla_due_at = due_at
    record.status = "ESCALATED"
    aid = _audit("system", "record.escalate", record,
                 {"fromLevel": frm_level, "toLevel": level,
                  "fromDesk": frm_desk, "toDesk": desk_id, "reason": reason})
    record.add_event(TimelineEvent(
        at=_now(), actor="system", action="escalated",
        from_status="ASSIGNED", to_status="ESCALATED", level=level, system=True,
        note=f"{reason}. Escalated L{frm_level}→L{level} to "
             f"{sla.desk_label(desk_id)}.", audit_event_id=aid))
    records_store.save(record)
    await _notify(record, "escalated",
                  f"⏫ {record.record_id} escalated to {sla.desk_label(desk_id)} "
                  f"(Level {level}) — {reason.lower()}.")
    log.info("Escalated %s L%d→L%d desk=%s", record.record_id, frm_level, level, desk_id)
    return record


# ---------------------------------------------------------------------------
# Citizen self-service
# ---------------------------------------------------------------------------

async def add_note(record: Record, *, actor: str, note: str,
                   attachments: Optional[list] = None) -> Record:
    if attachments:
        record.attachments.extend(attachments)
    aid = _audit(actor, "record.note", record, {"len": len(note)})
    record.add_event(TimelineEvent(
        at=_now(), actor=actor, action="note_added",
        from_status=record.status, to_status=record.status,
        level=record.current_level, note=note[:500], audit_event_id=aid))
    records_store.save(record)
    return record


async def send_reminder(record: Record, *, actor: str) -> Record:
    """Jansunwai-style 'nudge' — bumps priority + pings the owner desk."""
    if record.priority == "normal":
        record.priority = "high"
    record.extra["reminders"] = int(record.extra.get("reminders", 0)) + 1
    aid = _audit(actor, "record.reminder", record,
                 {"count": record.extra["reminders"]})
    record.add_event(TimelineEvent(
        at=_now(), actor=actor, action="reminder_sent",
        from_status=record.status, to_status=record.status,
        level=record.current_level,
        note=f"Citizen sent reminder #{record.extra['reminders']}. "
             f"Priority now {record.priority}.", audit_event_id=aid))
    records_store.save(record)
    await _notify(record, "reminder",
                  f"🔔 Reminder logged on {record.record_id}. "
                  f"Owner desk: {sla.desk_label(record.owner_desk_id)}.")
    return record


async def submit_feedback(record: Record, *, actor: str, rating: int,
                          comment: str = "") -> Record:
    """CM-Helpline satisfaction loop: ≥4 → CLOSED, ≤2 → REOPEN."""
    rating = max(1, min(5, int(rating)))
    record.satisfaction = rating
    record.extra["feedback_comment"] = comment[:500]
    if rating >= 4:
        return await transition(
            record, to_status="CLOSED", actor=actor, action="closed",
            note=f"Citizen satisfied (rated {rating}/5). Closed.")
    # dissatisfied → reopen at the same level with a fresh clock
    return await reopen(record, actor=actor,
                        reason=f"Citizen dissatisfied (rated {rating}/5)")


async def reopen(record: Record, *, actor: str, reason: str = "") -> Record:
    # fresh SLA clock at the current level
    nxt = sla.next_level(record.sla_policy_id or "", record.current_level - 1)
    if nxt:
        _, due_at, _ = nxt
        record.sla_due_at = due_at
    record.sla_breached = False
    record.resolution = None
    record.satisfaction = None
    aid = _audit(actor, "record.reopen", record, {"reason": reason})
    frm = record.status
    record.status = "ASSIGNED"
    record.add_event(TimelineEvent(
        at=_now(), actor=actor, action="reopened", from_status=frm,
        to_status="ASSIGNED", level=record.current_level,
        note=reason or "Reopened by citizen.", audit_event_id=aid))
    records_store.save(record)
    await _notify(record, "reopened",
                  f"🔄 {record.record_id} reopened — {reason or 'fresh review started'}.")
    return record


async def withdraw(record: Record, *, actor: str, reason: str = "") -> Record:
    return await transition(record, to_status="WITHDRAWN", actor=actor,
                            action="withdrawn",
                            note=reason or "Withdrawn by citizen.")


# ---------------------------------------------------------------------------
# Tracking (Jansunwai / NCH no-login lookup)
# ---------------------------------------------------------------------------

def track(record_id: str, *, msisdn: Optional[str] = None) -> Optional[dict]:
    rec = records_store.get(record_id)
    if not rec:
        return None
    if msisdn is not None:
        if rec.msisdn_hash != msisdn_hash(msisdn):
            return {"error": "mobile_mismatch"}
    return rec.public_view()
