"""Records / case-management API — Phase 6e.

Citizen self-service + public tracking + an admin/officer operations surface.
Public tracking (GET /api/v1/track/{id}) needs no login — exactly like UP
Jansunwai and the National Consumer Helpline.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .records.store import records_store
from .records import service as rsvc
from .records import sla as rsla
from . import auth as _auth

router = APIRouter(prefix="/api/v1", tags=["records"])


def _owned_or_404(record_id: str, claimed_citizen: str, token_citizen):
    """Fetch a record and enforce that `claimed_citizen` owns it. Closes the
    IDOR where anyone could act on any reference number."""
    r = records_store.get(record_id)
    if not r:
        raise HTTPException(404, "record not found")
    _auth.assert_owns(token_citizen, claimed_citizen)
    if r.citizen_id != claimed_citizen:
        # Don't reveal existence to a non-owner.
        raise HTTPException(404, "record not found")
    return r


# ---------------------------------------------------------------------------
# Public tracking (no login) — Jansunwai / NCH style
# ---------------------------------------------------------------------------

@router.get("/track/{record_id}")
async def public_track(record_id: str, mobile: str | None = Query(default=None)):
    view = rsvc.track(record_id, msisdn=mobile)
    if not view:
        raise HTTPException(404, "No record found for that reference number.")
    if view.get("error") == "mobile_mismatch":
        raise HTTPException(403, "Mobile number does not match this reference number.")
    return view


# ---------------------------------------------------------------------------
# Citizen-scoped
# ---------------------------------------------------------------------------

@router.get("/citizens/{citizen_id}/records")
async def list_records(citizen_id: str, kind: str = "", open_only: bool = False):
    recs = records_store.for_citizen(citizen_id, kind=kind, open_only=open_only)
    return {"citizenId": citizen_id, "count": len(recs),
            "records": [r.public_view() for r in recs]}


@router.get("/records/{record_id}")
async def get_record(record_id: str):
    r = records_store.get(record_id)
    if not r:
        raise HTTPException(404, "record not found")
    return r.public_view()


# Phase 6e — citizen self-service now requires the OWNING citizen_id and
# enforces ownership (was an open IDOR by reference number).
class NoteReq(BaseModel):
    note: str
    citizen_id: str


class ReminderReq(BaseModel):
    citizen_id: str


class FeedbackReq(BaseModel):
    rating: int
    comment: str = ""
    citizen_id: str


class ReasonReq(BaseModel):
    reason: str = ""
    citizen_id: str


@router.post("/records/{record_id}/note")
async def add_note(record_id: str, body: NoteReq,
                   tok=Depends(_auth.citizen_from_token)):
    r = _owned_or_404(record_id, body.citizen_id, tok)
    await rsvc.add_note(r, actor=body.citizen_id, note=body.note)
    return r.public_view()


@router.post("/records/{record_id}/reminder")
async def send_reminder(record_id: str, body: ReminderReq,
                        tok=Depends(_auth.citizen_from_token)):
    r = _owned_or_404(record_id, body.citizen_id, tok)
    await rsvc.send_reminder(r, actor=body.citizen_id)
    return r.public_view()


@router.post("/records/{record_id}/feedback")
async def submit_feedback(record_id: str, body: FeedbackReq,
                          tok=Depends(_auth.citizen_from_token)):
    r = _owned_or_404(record_id, body.citizen_id, tok)
    await rsvc.submit_feedback(r, actor=body.citizen_id,
                               rating=body.rating, comment=body.comment)
    return r.public_view()


@router.post("/records/{record_id}/reopen")
async def reopen(record_id: str, body: ReasonReq,
                 tok=Depends(_auth.citizen_from_token)):
    r = _owned_or_404(record_id, body.citizen_id, tok)
    await rsvc.reopen(r, actor=body.citizen_id, reason=body.reason)
    return r.public_view()


@router.post("/records/{record_id}/withdraw")
async def withdraw(record_id: str, body: ReasonReq,
                   tok=Depends(_auth.citizen_from_token)):
    r = _owned_or_404(record_id, body.citizen_id, tok)
    await rsvc.withdraw(r, actor=body.citizen_id, reason=body.reason)
    return r.public_view()


# ---------------------------------------------------------------------------
# Admin / officer operations
# ---------------------------------------------------------------------------

class TransitionReq(BaseModel):
    to_status: str
    action: str = "updated"
    note: str = ""
    resolution: str = ""
    actor: str = "officer"


def _get_or_404(record_id: str):
    r = records_store.get(record_id)
    if not r:
        raise HTTPException(404, "record not found")
    return r


@router.get("/admin/records")
async def admin_list(department: str = "", status: str = "",
                     state: str = "", level: int = 0,
                     _adm=Depends(_auth.require_admin)):
    recs = records_store.query(department_id=department, status=status,
                               state_code=state, level=level)
    return {"count": len(recs), "stats": records_store.stats(),
            "demoClock": rsla.demo_clock(),
            "records": [{**r.public_view(),
                         "ownerDesk": rsla.desk_label(r.owner_desk_id),
                         "ownerDeskId": r.owner_desk_id,
                         "citizenId": r.citizen_id} for r in recs]}


@router.get("/admin/records/stats")
async def admin_stats():
    return records_store.stats()


@router.post("/admin/records/{record_id}/transition")
async def admin_transition(record_id: str, body: TransitionReq,
                           _adm=Depends(_auth.require_admin)):
    r = _get_or_404(record_id)
    await rsvc.transition(r, to_status=body.to_status, actor=body.actor,
                          action=body.action, note=body.note,
                          resolution=body.resolution)
    return r.public_view()


@router.post("/admin/records/{record_id}/resolve")
async def admin_resolve(record_id: str, body: TransitionReq,
                        _adm=Depends(_auth.require_admin)):
    r = _get_or_404(record_id)
    await rsvc.transition(r, to_status="RESOLVED", actor=body.actor,
                          action="resolved",
                          note=body.note or "Marked resolved by officer.",
                          resolution=body.resolution or body.note)
    return r.public_view()


@router.post("/admin/records/{record_id}/escalate")
async def admin_escalate(record_id: str, _adm=Depends(_auth.require_admin)):
    r = _get_or_404(record_id)
    await rsvc.escalate(r, reason="Manual escalation by officer")
    return r.public_view()


@router.get("/admin/desks")
async def admin_desks():
    return rsla.all_desks_json()


@router.get("/admin/sla-policies")
async def admin_sla_policies():
    return rsla.all_policies_json()
