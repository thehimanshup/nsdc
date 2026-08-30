"""Scheme catalog API — Phase 6e."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import schemes
from .store import store
from .records import service as rsvc

router = APIRouter(prefix="/api/v1", tags=["schemes"])


@router.get("/schemes")
async def list_schemes(family: str = "", state: str = "", q: str = ""):
    if q or family or state:
        res = schemes.search(q, state_code=state, family=family, limit=50)
    else:
        res = schemes.all_schemes()
    return {"count": len(res), "families": schemes.families_json(), "schemes": res}


@router.get("/schemes/families")
async def list_families():
    return {"families": schemes.families_json()}


@router.get("/schemes/{scheme_id}")
async def get_scheme(scheme_id: str):
    s = schemes.get(scheme_id)
    if not s:
        raise HTTPException(404, "scheme not found")
    return s


class EligibilityReq(BaseModel):
    profile: dict = {}
    citizen_id: str | None = None


@router.post("/schemes/{scheme_id}/check-eligibility")
async def check_eligibility(scheme_id: str, body: EligibilityReq):
    profile = dict(body.profile or {})
    if body.citizen_id:
        c = store.get_citizen(body.citizen_id) or {}
        merged = dict(c.get("profile", {}))
        merged.update(profile)
        profile = merged
        profile.setdefault("state_code", c.get("state_code", ""))
    res = schemes.check_eligibility(scheme_id, profile)
    if res.get("error"):
        raise HTTPException(404, "scheme not found")
    return res


class ApplyReq(BaseModel):
    citizen_id: str


@router.post("/schemes/{scheme_id}/apply")
async def apply(scheme_id: str, body: ApplyReq):
    s = schemes.get(scheme_id)
    if not s:
        raise HTTPException(404, "scheme not found")
    c = store.get_citizen(body.citizen_id) or {}
    rec = rsvc.create_record(
        kind="scheme_application", citizen_id=body.citizen_id,
        msisdn=c.get("msisdn", ""), state_code=c.get("state_code", "TN"),
        department_id=s.get("owning_department", "social"),
        category="scheme.application", title=f"Application: {s['name']}",
        description=f"Application to {s['name']} ({scheme_id}).",
        scheme_id=scheme_id, initial_status="SUBMITTED",
        extra={"scheme_name": s["name"],
               "documents_required": s.get("documents_required", [])},
    )
    return {"recordId": rec.record_id, "status": rec.status,
            "scheme": s["name"],
            "documentsRequired": s.get("documents_required", []),
            "trackAt": f"/api/v1/track/{rec.record_id}"}


# --- admin catalog ---------------------------------------------------------

@router.get("/admin/schemes")
async def admin_schemes():
    return {"count": len(schemes.all_schemes()),
            "families": schemes.families_json(),
            "schemes": schemes.all_schemes()}
