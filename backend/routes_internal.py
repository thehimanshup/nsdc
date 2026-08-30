"""Internal tool-execution API — Phase 6g.

Lets the LiveKit voice worker (a SEPARATE process) execute agent tools
inside the MAIN backend process. This matters because the records/store
layers are JSON-file backed with in-memory indexes loaded once at startup:
if the worker wrote records.json directly, the backend would never see the
new record and would silently overwrite it on its next persist. Routing all
tool execution through this endpoint keeps a single writer process, so a
complaint registered on a phone call shows up on the admin backend
immediately — with a real reference number.

Security: requires the INTERNAL_API_KEY shared secret when set; otherwise
only loopback callers are accepted (local dev default).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .store import store
from .tools import get_tool

log = logging.getLogger("internal.tools")

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

_INTERNAL_KEY = os.getenv("INTERNAL_API_KEY", "")


def _check_auth(request: Request) -> None:
    if _INTERNAL_KEY:
        if request.headers.get("x-internal-key", "") != _INTERNAL_KEY:
            raise HTTPException(403, "bad internal key")
        return
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "internal API is loopback-only "
                                 "(set INTERNAL_API_KEY to call remotely)")


class TurnMetricsReq(BaseModel):
    agent_id: str
    channel: str = "livekit_app"
    conv_id: str = ""
    stages: dict = {}            # ms: stt, llm_first, llm_total, tts, total…
    lang: str = ""
    voice: bool = True
    tool: str = ""


@router.post("/metrics/turn")
async def record_turn_metrics(req: TurnMetricsReq, request: Request) -> dict:
    """Phase 6h — lets the LiveKit voice worker (separate process) feed its
    per-turn latency (from livekit-agents metrics events) into the same
    store the admin dashboard reads."""
    _check_auth(request)
    from . import latency_metrics as _lat
    ev = _lat.record_turn(
        conv_id=req.conv_id or f"call-{req.agent_id}", agent_id=req.agent_id,
        channel=req.channel, stages={k: float(v) for k, v in
                                     (req.stages or {}).items()
                                     if isinstance(v, (int, float))},
        speak_reply=req.voice, lang=req.lang, tool_id=req.tool,
    )
    return {"ok": True, "ts": ev["ts"]}


class ToolExecReq(BaseModel):
    agent_id: str
    tool_id: str
    args: dict = {}
    # Caller identity — msisdn from call metadata, or a per-room guest id.
    msisdn: str = ""
    citizen_id: str = ""
    language: str = "en-IN"
    state_code: str = ""
    channel: str = "voice_call"


@router.post("/tools/execute")
async def execute_tool(req: ToolExecReq, request: Request) -> dict:
    _check_auth(request)
    tool = get_tool(req.tool_id)
    if not tool:
        raise HTTPException(404, f"unknown tool: {req.tool_id}")
    # Authorize against the agent's EFFECTIVE tool set (operator bindings +
    # skill-bundled tools) — the same gate the chat path uses — so skill/MCP
    # tools (whose in-code allowed_agents is empty) are permitted on voice too.
    from .orchestrator import _agent_tools
    if not any(t.id == req.tool_id for t in _agent_tools(req.agent_id)):
        raise HTTPException(403, f"tool {req.tool_id} not allowed for agent "
                                 f"{req.agent_id}")

    citizen_id = (req.citizen_id or "").strip()
    if not citizen_id:
        # Resolve / create the citizen from the call's msisdn so call-side
        # records attach to the same citizen as chat-side ones.
        msisdn = (req.msisdn or "").strip() or f"voice-{req.agent_id}"
        citizen_id = store.get_or_create_citizen(msisdn)
        c = store.get_citizen(citizen_id) or {}
        if req.language and not c.get("language"):
            c["language"] = req.language
        if req.state_code and not c.get("state_code"):
            c["state_code"] = req.state_code

    args = dict(req.args or {})
    args.setdefault("_channel", req.channel)
    try:
        result = await tool.execute(args, citizen_id)
    except Exception as e:  # noqa: BLE001 — surface tool errors to the worker
        log.exception("internal tool %s failed", req.tool_id)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    log.info("internal tool %s by agent=%s citizen=%s -> ok=%s",
             req.tool_id, req.agent_id, citizen_id,
             result.get("ok") if isinstance(result, dict) else "?")
    return {"ok": True, "citizen_id": citizen_id, "result": result}


class ToolsForAgentReq(BaseModel):
    agent_id: str


@router.post("/tools/for-agent")
async def tools_for_agent_ep(req: ToolsForAgentReq, request: Request) -> dict:
    """List the tools an agent may use this turn — built-ins + drop-in plugins +
    MCP tools + skill-bundled tools (exactly what the chat agent sees via
    `_agent_tools`) — plus its attached skills' instruction block.

    The LiveKit voice worker calls this so it can expose the SAME dynamic tool
    set the chat agent has, instead of a hardcoded list. Consent-gated tools are
    omitted (a live voice call has no tap-to-allow consent modal).
    """
    _check_auth(request)
    from .orchestrator import _agent_tools
    tools = []
    for t in _agent_tools(req.agent_id):
        if t.requires_consent:
            continue  # skip permission-asking tools on voice
        tools.append({
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "input_schema": (t.input_schema
                             or {"type": "object", "properties": {}, "required": []}),
            "source": getattr(t, "source", "builtin"),
        })
    # Skill instruction block — mirror the orchestrator's prompt injection so the
    # voice agent gets the same "how to use these tools" guidance.
    skill_instructions = ""
    try:
        from .config import settings as _s
        if _s.skills_enabled:
            from .skills import skills_for_agent
            blocks = [f"- {s.name}: {s.instructions}".rstrip(": ").rstrip()
                      for s in skills_for_agent(req.agent_id)[:4] if s.instructions]
            if blocks:
                skill_instructions = ("\n\nSKILLS — extra capabilities available "
                                      "to you this call:\n" + "\n".join(blocks))
    except Exception:
        log.exception("skill instruction build failed for %s", req.agent_id)
    return {"ok": True, "tools": tools, "skill_instructions": skill_instructions}
