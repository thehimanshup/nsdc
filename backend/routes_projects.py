"""Development-project tracking API — Phase 6e."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import projects
from .store import store
from .records import service as rsvc

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects")
async def list_projects(state: str = "", district: str = "", type: str = "",
                        q: str = ""):
    res = projects.find(state_code=state, district=district, ptype=type, query=q)
    return {"count": len(res),
            "projects": [projects.summary(p["project_id"]) for p in res]}


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    s = projects.summary(project_id)
    if not s:
        raise HTTPException(404, "project not found")
    return s


class ReportReq(BaseModel):
    citizen_id: str
    description: str = ""
    location: str = ""
    category: str = "road_defect"


@router.post("/projects/{project_id}/report-issue")
async def report_issue(project_id: str, body: ReportReq):
    proj = projects.get(project_id)
    if not proj:
        raise HTTPException(404, "project not found")
    c = store.get_citizen(body.citizen_id) or {}
    rec = rsvc.create_record(
        kind="grievance", citizen_id=body.citizen_id, msisdn=c.get("msisdn", ""),
        state_code=c.get("state_code", proj.get("state_code", "TN")),
        department_id=proj.get("department", "pwd"),
        category=body.category, title=f"Issue: {proj['name']}",
        description=body.description or f"Citizen reported an issue with {proj['name']}.",
        district=proj.get("district"), ward_block=proj.get("ward_block"),
        project_id=project_id, priority="normal",
        extra={"projectName": proj["name"]},
    )
    return {"recordId": rec.record_id, "status": rec.status,
            "project": proj["name"], "trackAt": f"/api/v1/track/{rec.record_id}"}


# --- admin catalog ---------------------------------------------------------

@router.get("/admin/projects")
async def admin_projects():
    return {"count": len(projects.all_projects()),
            "projects": [projects.summary(p["project_id"])
                         for p in projects.all_projects()]}
