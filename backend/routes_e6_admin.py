"""Phase 6e admin extensions — workflow templates + agent templates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import workflows as _wf
from . import agent_templates as _at
from . import auth as _auth

router = APIRouter(prefix="/api/v1/admin", tags=["admin-6e"])


@router.get("/workflows")
async def list_workflows():
    return {"count": len(_wf.all_templates()), "workflows": _wf.all_templates()}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    w = _wf.get(workflow_id)
    if not w:
        raise HTTPException(404, "workflow not found")
    return w


@router.get("/agent-templates")
async def list_agent_templates():
    return {"templates": _at.list_templates()}


class FromTemplateReq(BaseModel):
    template_id: str
    agent_id: str
    name: str
    emoji: str = ""
    color: str = ""
    bg: str = ""
    persona_name: str = ""
    state_scope: str = ""
    department_block: str = ""
    enabled: bool = True


@router.post("/agents/from-template")
async def create_from_template(body: FromTemplateReq,
                               _adm=Depends(_auth.require_admin)):
    agent = _at.instantiate(
        template_id=body.template_id, agent_id=body.agent_id, name=body.name,
        emoji=body.emoji, color=body.color, bg=body.bg,
        persona_name=body.persona_name, state_scope=body.state_scope,
        department_block=body.department_block, enabled=body.enabled,
    )
    if not agent:
        raise HTTPException(400, "unknown template_id")
    return {"ok": True, "agentId": agent.id, "name": agent.name,
            "tools": agent.tool_ids}
