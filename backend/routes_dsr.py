"""Data Subject Rights endpoints (citizen-facing) + admin views.

Citizen-facing:
  GET    /api/v1/citizen/{cid}/export             ZIP archive of all their data
  GET    /api/v1/citizen/{cid}/consents           list active consents
  POST   /api/v1/citizen/{cid}/consents/{grantId}/revoke
  POST   /api/v1/citizen/{cid}/correction         submit a correction request
  POST   /api/v1/citizen/{cid}/erasure            submit erasure request
  GET    /api/v1/citizen/{cid}/audit              audit entries referencing this citizen

Admin-facing:
  GET    /api/v1/admin/dsr/requests               list all DSR requests for DPO review
  GET    /api/v1/admin/audit/events               paginated audit log
  GET    /api/v1/admin/audit/verify               verify the audit chain
  GET    /api/v1/admin/audit/roots                list daily Merkle roots
  GET    /api/v1/admin/consent-ledger/verify      verify the consent chain
  GET    /api/v1/admin/consent-ledger/entries     paginated ledger entries
  GET    /api/v1/admin/crypto/info                show active crypto backend
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import audit, consent, crypto_utils, dsr
from .config import settings
from .store import store

log = logging.getLogger("dsr.routes")
router = APIRouter()


# ---------------------------------------------------------------------------
# Citizen-facing
# ---------------------------------------------------------------------------

@router.get("/api/v1/citizen/{citizen_id}/export")
async def citizen_export(citizen_id: str) -> Response:
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    zip_bytes = dsr.export_citizen_data(citizen_id)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="my-data-{citizen_id}.zip"',
        },
    )


@router.get("/api/v1/citizen/{citizen_id}/consents")
async def citizen_consents(citizen_id: str) -> dict:
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    active = consent.list_active_for_citizen(citizen_id)
    all_entries = consent.list_all_for_citizen(citizen_id)
    return {
        "citizenId": citizen_id,
        "active_count": len(active),
        "all_count": len(all_entries),
        "active": [
            {
                "grantId": c.grant_id, "requestId": c.request_id,
                "agentId": c.agent_id, "toolId": c.tool_id,
                "scope": c.scope, "purpose": c.purpose,
                "decidedAt": c.decided_at.isoformat() if c.decided_at else None,
                "expiresAt": c.expires_at.isoformat() if c.expires_at else None,
            }
            for c in active
        ],
        "history": [
            {
                "entryNo": e.get("entryNo"),
                "type": e.get("type"),
                "requestId": e.get("requestId"),
                "grantId": e.get("grantId"),
                "agentId": e.get("agentId"),
                "toolId": e.get("toolId"),
                "scope": e.get("scope"),
                "status": e.get("status"),
                "requestedAt": e.get("requestedAt"),
                "decidedAt": e.get("decidedAt"),
                "expiresAt": e.get("expiresAt"),
            }
            for e in all_entries
        ],
    }


@router.post("/api/v1/citizen/{citizen_id}/consents/{grant_id}/revoke")
async def citizen_revoke_consent(citizen_id: str, grant_id: str) -> dict:
    result = consent.revoke(grant_id, citizen_id)
    if not result:
        raise HTTPException(404, "consent grant not found or already inactive")
    audit.append_event(
        actor=citizen_id, action="consent.revoke",
        resource={"citizenId": citizen_id, "grantId": grant_id,
                  "scope": result.scope},
    )
    return {"ok": True, "status": result.status,
            "revoked_at": result.decided_at.isoformat() if result.decided_at else None}


class CorrectionRequest(BaseModel):
    field: str = Field(..., description="profile field to correct")
    new_value: str
    reason: str = ""


@router.post("/api/v1/citizen/{citizen_id}/correction")
async def citizen_correction(citizen_id: str, req: CorrectionRequest) -> dict:
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    return dsr.submit_correction(citizen_id, req.field, req.new_value, req.reason)


class ErasureRequest(BaseModel):
    reason: str = ""
    confirm: bool = False


@router.post("/api/v1/citizen/{citizen_id}/erasure")
async def citizen_erasure(citizen_id: str, req: ErasureRequest) -> dict:
    if not req.confirm:
        raise HTTPException(400, "Set confirm=true to proceed. Erasure is irreversible.")
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    return dsr.submit_erasure(citizen_id, req.reason)


@router.get("/api/v1/citizen/{citizen_id}/audit")
async def citizen_audit_view(citizen_id: str, limit: int = 100) -> dict:
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    entries = audit.query_for_citizen(citizen_id, limit=limit)
    return {"citizenId": citizen_id, "count": len(entries), "entries": entries}


# ---------------------------------------------------------------------------
# Admin-facing (DPO + auditor views)
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/dsr/requests")
async def admin_list_dsr() -> dict:
    return {"requests": dsr.list_dsr_requests()}


@router.get("/api/v1/admin/audit/events")
async def admin_audit_events(actor: Optional[str] = None,
                             action: Optional[str] = None,
                             since: Optional[str] = None,
                             limit: int = Query(default=100, le=1000)) -> dict:
    entries = audit.query(actor=actor, action=action, since=since, limit=limit)
    return {"count": len(entries), "entries": entries}


@router.get("/api/v1/admin/audit/verify")
async def admin_audit_verify() -> dict:
    return audit.verify_chain()


@router.get("/api/v1/admin/audit/roots")
async def admin_audit_roots() -> dict:
    roots_dir = Path(settings.data_dir) / "audit" / "roots"
    if not roots_dir.exists():
        return {"roots": []}
    roots = []
    for p in sorted(roots_dir.glob("*.json"), reverse=True)[:60]:
        try:
            import json
            roots.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return {"roots": roots}


@router.post("/api/v1/admin/audit/compute-root")
async def admin_compute_root() -> dict:
    return audit.compute_daily_root()


@router.get("/api/v1/admin/consent-ledger/verify")
async def admin_consent_verify() -> dict:
    return consent.verify_chain()


@router.get("/api/v1/admin/consent-ledger/entries")
async def admin_consent_entries(citizen_id: Optional[str] = None,
                                 limit: int = Query(default=100, le=1000)) -> dict:
    if citizen_id:
        entries = consent.list_all_for_citizen(citizen_id)
    else:
        # Walk the in-memory cache
        from .consent import _REQUESTS
        entries = sorted(list(_REQUESTS.values()),
                          key=lambda e: e.get("entryNo", 0),
                          reverse=True)[:limit]
    return {"count": len(entries), "entries": entries}


@router.get("/api/v1/admin/crypto/info")
async def admin_crypto_info() -> dict:
    return crypto_utils.backend_info()
