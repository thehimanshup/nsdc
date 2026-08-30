"""Admin Console API routes.

Endpoints:
  GET    /api/v1/admin/agents                  list all agents
  GET    /api/v1/admin/agents/{id}             one agent
  POST   /api/v1/admin/agents                  create new agent
  PATCH  /api/v1/admin/agents/{id}             update agent
  DELETE /api/v1/admin/agents/{id}             remove agent
  POST   /api/v1/admin/agents/reset            wipe + re-seed from defaults

  GET    /api/v1/admin/tools                   list tool registry (read-only)
  GET    /api/v1/admin/voices                  list Bulbul voices
  POST   /api/v1/admin/voices/preview          synthesize a sample line

  GET    /api/v1/admin/broadcasts              list all broadcasts
  POST   /api/v1/admin/broadcasts              compose new broadcast
  POST   /api/v1/admin/broadcasts/{id}/approve approve (four-eyes)
  POST   /api/v1/admin/broadcasts/{id}/reject  reject
  POST   /api/v1/admin/broadcasts/{id}/send    fan-out to citizens

  GET    /api/v1/admin/coordinator/recipes     list multi-agent flows
  POST   /api/v1/admin/agents/{id}/sandbox     one-off test message

  GET    /api/v1/admin/metrics                 simple stats

In production this entire router would sit behind an admin-only auth
guard (SAML/OIDC + four-eyes RBAC). Phase 5 leaves it open for local dev.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import admin_storage, broadcasts as bcast, coordinator as coord
from .agents import Agent, AGENTS, all_agents, get_agent
from .config import settings
from .store import store
from .tools import all_tools, tools_for_agent
from .voice import tts_synthesize

log = logging.getLogger("admin.routes")
router = APIRouter()


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

class AgentCreate(BaseModel):
    id: str = Field(..., min_length=2, max_length=32)
    name: str
    emoji: str = "🏢"
    color: str = "#075E54"
    bg: str = "#e0f2f1"
    description: str = ""
    department_block: str = ""
    voice: str = "shubh"
    voice_pool: list[str] = Field(default_factory=list)
    persona_variants: list[dict] = Field(default_factory=list)
    pinned: bool = False
    enabled: bool = True                    # Phase 6d — start enabled by default
    tool_ids: list[str] = Field(default_factory=list)
    mock_responses: list[str] = Field(default_factory=list)
    push_pool: list[str] = Field(default_factory=list)
    corpus_id: Optional[str] = None
    llm_provider: Optional[str] = None      # Phase 6b — per-agent LLM override
    # Phase 6c — persona fields
    persona_name: str = ""
    tone: str = "warm-helpful"
    signature_opener: str = ""
    signature_closer: str = ""
    conversational_traits: list[str] = Field(default_factory=list)
    cross_corpus_read: list[str] = Field(default_factory=list)


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    color: Optional[str] = None
    bg: Optional[str] = None
    description: Optional[str] = None
    department_block: Optional[str] = None
    voice: Optional[str] = None
    voice_pool: Optional[list[str]] = None
    persona_variants: Optional[list[dict]] = None
    pinned: Optional[bool] = None
    enabled: Optional[bool] = None          # Phase 6d
    tool_ids: Optional[list[str]] = None
    mock_responses: Optional[list[str]] = None
    push_pool: Optional[list[str]] = None
    corpus_id: Optional[str] = None
    llm_provider: Optional[str] = None      # Phase 6b — per-agent LLM override
    # Phase 6c — persona fields
    persona_name: Optional[str] = None
    tone: Optional[str] = None
    signature_opener: Optional[str] = None
    signature_closer: Optional[str] = None
    conversational_traits: Optional[list[str]] = None
    cross_corpus_read: Optional[list[str]] = None


class VoicePreviewRequest(BaseModel):
    voice: str = "shubh"
    language: str = "en-IN"
    text: str = "Hello, this is a sample of how I'd sound."


class BroadcastCreate(BaseModel):
    agentId: str
    title: str
    body: str
    targetAudience: dict = Field(default_factory=lambda: {"type": "all"})
    languages: list[str] = Field(default_factory=list)
    composedBy: str = "officer-1"
    autoTranslate: bool = False


class ApproveRequest(BaseModel):
    by: str = "officer-2"


class SandboxMessage(BaseModel):
    text: str
    lang: str = "en-IN"


# ---------------------------------------------------------------------------
# AGENTS — CRUD
# ---------------------------------------------------------------------------

def _agent_to_dict(a: Agent) -> dict:
    return {
        "id": a.id, "name": a.name, "emoji": a.emoji,
        "color": a.color, "bg": a.bg,
        "description": a.description,
        "department_block": a.department_block,
        "voice": a.voice, "pinned": a.pinned,
        "voice_pool": list(getattr(a, "voice_pool", []) or []),
        "persona_variants": list(getattr(a, "persona_variants", []) or []),
        "enabled": getattr(a, "enabled", True),
        "tool_ids": a.tool_ids,
        "mock_responses": a.mock_responses,
        "push_pool": a.push_pool,
        "corpus_id": a.corpus_id,
        "llm_provider": getattr(a, "llm_provider", None),
        # Phase 6c — persona fields
        "persona_name": getattr(a, "persona_name", "") or "",
        "tone": getattr(a, "tone", "") or "",
        "signature_opener": getattr(a, "signature_opener", "") or "",
        "signature_closer": getattr(a, "signature_closer", "") or "",
        "conversational_traits": list(getattr(a, "conversational_traits", []) or []),
        "cross_corpus_read": list(getattr(a, "cross_corpus_read", []) or []),
    }


@router.get("/api/v1/admin/agents")
async def admin_list_agents() -> dict:
    return {"agents": [_agent_to_dict(a) for a in all_agents()]}


@router.get("/api/v1/admin/agents/{agent_id}")
async def admin_get_agent(agent_id: str) -> dict:
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return _agent_to_dict(a)


@router.post("/api/v1/admin/agents")
async def admin_create_agent(req: AgentCreate) -> dict:
    if req.id in AGENTS:
        raise HTTPException(409, f"agent '{req.id}' already exists")
    a = Agent(
        id=req.id, name=req.name, emoji=req.emoji,
        color=req.color, bg=req.bg,
        description=req.description,
        department_block=req.department_block or req.description,
        voice=req.voice, pinned=req.pinned,
        voice_pool=req.voice_pool,
        persona_variants=req.persona_variants or [],
        tool_ids=req.tool_ids,
        mock_responses=req.mock_responses or [f"Welcome to {req.name}. How may I help?"],
        push_pool=req.push_pool or [],
        corpus_id=req.corpus_id,
        cross_corpus_read=req.cross_corpus_read,
        llm_provider=req.llm_provider,
    )
    admin_storage.save_agent(a)
    return {"ok": True, "agent": _agent_to_dict(a)}


@router.patch("/api/v1/admin/agents/{agent_id}")
async def admin_update_agent(agent_id: str, req: AgentUpdate) -> dict:
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    updates = req.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(a, k, v)
    admin_storage.save_agent(a)
    return {"ok": True, "agent": _agent_to_dict(a)}


@router.delete("/api/v1/admin/agents/{agent_id}")
async def admin_delete_agent(agent_id: str) -> dict:
    if not admin_storage.delete_agent(agent_id):
        raise HTTPException(404, "agent not found")
    return {"ok": True, "deleted": agent_id}


@router.post("/api/v1/admin/agents/reset")
async def admin_reset_agents() -> dict:
    if not settings.allow_demo_routes:
        raise HTTPException(403, "agent reset/reseed is disabled in production")
    n = admin_storage.reset_to_defaults()
    return {"ok": True, "count": n}


class EnableRequest(BaseModel):
    enabled: bool


@router.post("/api/v1/admin/agents/{agent_id}/enable")
async def admin_enable_agent(agent_id: str, req: EnableRequest) -> dict:
    """Phase 6d — quick-toggle endpoint to take an agent offline / back online
    without going through the full edit modal.

    Disabling an agent:
      - hides it from the citizen-facing /api/v1/agents response
      - blocks new messages (text + voice + image) with HTTP 503
      - preserves all existing conversation history
      - writes an audit log entry so the DPO can see when departments were
        offline (relevant for SLA accounting under DPDP §13)
    """
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if a.enabled == req.enabled:
        return {"ok": True, "agent_id": agent_id, "enabled": a.enabled,
                "note": "no change"}
    a.enabled = req.enabled
    admin_storage.save_agent(a)
    # Audit
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="agent.enable" if req.enabled else "agent.disable",
        resource={"agentId": agent_id, "agentName": a.name},
        payload={"enabled": req.enabled},
    )
    return {"ok": True, "agent_id": agent_id, "enabled": a.enabled,
            "name": a.name}


# ---------------------------------------------------------------------------
# LLM PROVIDERS — list for per-agent dropdown
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/llm/providers")
async def admin_list_llm_providers() -> dict:
    """List all LLM providers available for per-agent override (Phase 6b)."""
    from .llm import list_providers, get_llm, get_llm_for
    default = get_llm().info().as_dict()
    out = []
    for p in list_providers():
        try:
            info = get_llm_for(p).info().as_dict()
        except Exception as e:
            info = {"name": p, "display_name": p, "is_sovereign": p == "sarvam",
                    "is_mock": True, "model_id": "?", "base_url": "",
                    "error": str(e)}
        out.append(info)
    return {
        "default": default,
        "providers": out,
        "sovereign_warning": (
            "Only 'sarvam' is India-sovereign. Other providers are overseas — "
            "use only for non-PII workflows or with explicit citizen consent."
        ),
    }


# ---------------------------------------------------------------------------
# TOOLS — editable: enable/disable, wire to agents, test, reload
# ---------------------------------------------------------------------------
#
# What a tool IS and DOES lives in code (built-in tools.py / drop-in plugins /
# MCP servers). WHETHER it's on and WHICH agents use it lives in
# data/tool_bindings.json, edited here. tools_for_agent() reads that file fresh
# on every chat turn, so a toggle/rewire takes effect on the next message.

def _tool_row(t) -> dict:
    """Serialise a Tool + its current binding (effective enabled/agents)."""
    from . import tool_bindings
    b = tool_bindings.get(t.id)
    return {
        "id": t.id, "name": t.name, "description": t.description,
        "connector": t.connector,
        "category": getattr(t, "category", "") or t.connector,
        "source": getattr(t, "source", "builtin"),
        "requires_consent": t.requires_consent,
        "consent_scope": t.consent_scope,
        "input_schema": t.input_schema,
        # default_agents = the developer's in-code suggestion
        "default_agents": t.allowed_agents,
        # binding = operator override (null = falls back to default_agents)
        "binding": b,
        # effective = what tools_for_agent() will actually use right now
        "enabled": (b["enabled"] if b else True),
        "agents": (b["agents"] if b else t.allowed_agents),
    }


@router.get("/api/v1/admin/tools")
async def admin_list_tools() -> dict:
    return {"tools": [_tool_row(t) for t in all_tools()]}


class ToolBindingRequest(BaseModel):
    enabled: bool = True
    agents: list[str] = Field(default_factory=list)


@router.put("/api/v1/admin/tools/{tool_id}/binding")
async def admin_set_tool_binding(tool_id: str, req: ToolBindingRequest) -> dict:
    """Save the operator-controlled binding (on/off + agent wiring) for a tool.

    Takes effect on the next chat turn — both orchestration engines read
    tools_for_agent() fresh on every message. Audited for the DPO."""
    from . import tool_bindings
    from .tools import get_tool
    t = get_tool(tool_id)
    if not t:
        raise HTTPException(404, "tool not found")
    # Only allow wiring to agents that actually exist.
    unknown = [a for a in req.agents if a not in AGENTS]
    if unknown:
        raise HTTPException(400, f"unknown agent(s): {', '.join(unknown)}")
    saved = tool_bindings.set_binding(tool_id, enabled=req.enabled,
                                      agents=req.agents)
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="tool.binding",
        resource={"toolId": tool_id, "toolName": t.name},
        payload={"enabled": saved["enabled"], "agents": saved["agents"]},
    )
    return {"ok": True, "tool_id": tool_id, "binding": saved}


class ToolTestRequest(BaseModel):
    args: dict = Field(default_factory=dict)
    citizen_id: Optional[str] = None


@router.post("/api/v1/admin/tools/{tool_id}/test")
async def admin_test_tool(tool_id: str, req: ToolTestRequest) -> dict:
    """Run a tool with sample inputs against a throwaway citizen and return the
    raw JSON result, so an operator can validate a freshly dropped tool before
    enabling it. Consent/PII/audit inside execute() still apply."""
    from .tools import get_tool
    t = get_tool(tool_id)
    if not t:
        raise HTTPException(404, "tool not found")
    citizen_id = req.citizen_id or "ctz_admin_test"
    try:
        result = await t.execute(dict(req.args or {}), citizen_id)
        return {"ok": True, "tool_id": tool_id, "result": result}
    except Exception as e:  # noqa: BLE001 — surface the failure to the operator
        log.warning("Tool test failed for %s: %s", tool_id, e)
        return {"ok": False, "tool_id": tool_id, "error": str(e)}


@router.post("/api/v1/admin/tools/reload")
async def admin_reload_tools() -> dict:
    """Re-scan drop-in plugins and reconnect MCP servers without a restart."""
    from . import tool_loader, mcp_loader, tool_bindings
    plugins = tool_loader.reload()
    tool_bindings.load()        # pick up any out-of-band edits to the file
    mcp_loader.load()
    mcp_tools = await mcp_loader.connect_all()
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="tool.reload",
        resource={}, payload={"plugins": plugins, "mcp_tools": mcp_tools},
    )
    return {"ok": True, "plugins_loaded": plugins, "mcp_tools": mcp_tools,
            "total_tools": len(all_tools())}


# ---------------------------------------------------------------------------
# MCP SERVERS — connect external tool servers from the UI
# ---------------------------------------------------------------------------
#
# An MCP server's tool CODE lives on a separate machine; here we only store its
# ADDRESS (url + optional auth) in data/mcp_servers.json and connect as a
# client. Saving/deleting a server reconnects all servers, so its wrapper tools
# appear/disappear in the registry (and on the Tools page) without a restart.

class McpServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=48,
                      pattern=r"^[a-zA-Z0-9_\-]+$")
    url: str = Field(..., min_length=1)
    transport: str = "streamable_http"
    auth_token_env: str = ""
    enabled: bool = True
    tool_keywords: dict = Field(default_factory=dict)


class McpProbeRequest(BaseModel):
    url: str = Field(..., min_length=1)
    transport: str = "streamable_http"
    auth_token_env: str = ""


@router.get("/api/v1/admin/mcp/servers")
async def admin_list_mcp_servers() -> dict:
    """List configured MCP servers with their last-known connection status."""
    from . import mcp_loader
    return {"servers": mcp_loader.list_servers()}


@router.post("/api/v1/admin/mcp/test")
async def admin_test_mcp_connection(req: McpProbeRequest) -> dict:
    """Try to connect to a server WITHOUT saving it, and list its tools.
    Lets an operator validate an address before adding it."""
    from . import mcp_loader
    return await mcp_loader.probe(req.url, transport=req.transport,
                                  auth_token_env=req.auth_token_env)


@router.put("/api/v1/admin/mcp/servers")
async def admin_save_mcp_server(req: McpServerRequest) -> dict:
    """Add or update an MCP server, then reconnect all servers so its tools
    register into the live registry. Returns per-server connection status."""
    if req.transport not in ("streamable_http", "sse"):
        raise HTTPException(400, "transport must be 'streamable_http' or 'sse'")
    from . import mcp_loader
    mcp_loader.save_server(
        req.name, url=req.url, transport=req.transport,
        auth_token_env=req.auth_token_env, enabled=req.enabled,
        tool_keywords=req.tool_keywords)
    registered = await mcp_loader.connect_all()
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="mcp.server.save",
        resource={"server": req.name, "url": req.url},
        payload={"enabled": req.enabled, "transport": req.transport},
    )
    servers = mcp_loader.list_servers()
    this = next((s for s in servers if s["name"] == req.name), None)
    return {"ok": True, "server": this, "mcp_tools_registered": registered,
            "servers": servers}


@router.delete("/api/v1/admin/mcp/servers/{name}")
async def admin_delete_mcp_server(name: str) -> dict:
    """Remove an MCP server and reconnect (dropping its tools from the registry)."""
    from . import mcp_loader
    if not mcp_loader.delete_server(name):
        raise HTTPException(404, "mcp server not found")
    registered = await mcp_loader.connect_all()
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="mcp.server.delete",
        resource={"server": name}, payload={},
    )
    return {"ok": True, "deleted": name, "mcp_tools_registered": registered,
            "servers": mcp_loader.list_servers()}


# ---------------------------------------------------------------------------
# SKILLS — attachable bundles of tools + instructions (Phase 7)
# ---------------------------------------------------------------------------
#
# A skill IS data (data/skills/<id>.json): the tools it brings + an instruction
# fragment + an optional RAG corpus. WHETHER it's on and WHICH agents get it is
# operator wiring in data/skill_bindings.json. skills_for_agent() reads both
# fresh every turn, so an edit takes effect on the next message — no restart.

def _skill_row(s) -> dict:
    """Serialise a Skill + its binding (effective enabled/agents) for the UI."""
    from . import skill_bindings
    from .tools import get_tool
    b = skill_bindings.get(s.id)
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "instructions": s.instructions, "tool_ids": s.tool_ids,
        "corpus_id": s.corpus_id, "source": s.source,
        # default_agents = the skill author's suggestion
        "default_agents": s.default_agents,
        # binding = operator override (null = falls back to default_agents)
        "binding": b,
        # effective = what skills_for_agent() uses right now
        "enabled": (b["enabled"] if b else s.enabled),
        "agents": (b["agents"] if b else s.default_agents),
        # tool ids the skill references that aren't in the registry (e.g. an MCP
        # server not yet connected) — surfaced so the operator can see gaps.
        "missing_tools": [tid for tid in s.tool_ids if get_tool(tid) is None],
        # Callback Agent Platform — present + the contract when this is an
        # outbound callback skill, so the UI can route it to the Builder.
        "is_callback": bool(s.outbound),
        "outbound": s.outbound,
    }


@router.get("/api/v1/admin/skills")
async def admin_list_skills() -> dict:
    from . import skills
    return {"skills": [_skill_row(s) for s in skills.all_skills()],
            "enabled": settings.skills_enabled}


class SkillUpsertRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=48,
                    pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = Field(..., min_length=1)
    description: str = ""
    instructions: str = ""
    tool_ids: list[str] = Field(default_factory=list)
    corpus_id: str = ""
    default_agents: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Callback Agent Platform — the outbound conversation contract authored by
    # the admin Callback Builder. None = an ordinary inbound skill. Validated
    # by outbound_contract.validate_outbound inside save_skill.
    outbound: dict | None = None


@router.put("/api/v1/admin/skills")
async def admin_save_skill(req: SkillUpsertRequest) -> dict:
    """Create/update a skill (writes data/skills/<id>.json) and refresh."""
    from . import skills
    unknown = [a for a in req.default_agents if a not in AGENTS]
    if unknown:
        raise HTTPException(400, f"unknown agent(s): {', '.join(unknown)}")
    try:
        saved = skills.save_skill(
            id=req.id, name=req.name, description=req.description,
            instructions=req.instructions, tool_ids=req.tool_ids,
            corpus_id=req.corpus_id, default_agents=req.default_agents,
            enabled=req.enabled, outbound=req.outbound)
    except ValueError as e:                     # invalid outbound contract
        raise HTTPException(400, str(e))
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="skill.save",
        resource={"skillId": req.id, "skillName": req.name},
        payload={"tool_ids": saved.tool_ids, "enabled": saved.enabled},
    )
    return {"ok": True, "skill": _skill_row(saved)}


class SkillBindingRequest(BaseModel):
    enabled: bool = True
    agents: list[str] = Field(default_factory=list)


@router.put("/api/v1/admin/skills/{skill_id}/binding")
async def admin_set_skill_binding(skill_id: str, req: SkillBindingRequest) -> dict:
    """Save the operator-controlled wiring (on/off + agents) for a skill."""
    from . import skill_bindings, skills
    if skills.get_skill(skill_id) is None:
        raise HTTPException(404, "skill not found")
    unknown = [a for a in req.agents if a not in AGENTS]
    if unknown:
        raise HTTPException(400, f"unknown agent(s): {', '.join(unknown)}")
    saved = skill_bindings.set_binding(skill_id, enabled=req.enabled,
                                       agents=req.agents)
    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="skill.binding",
        resource={"skillId": skill_id},
        payload={"enabled": saved["enabled"], "agents": saved["agents"]},
    )
    return {"ok": True, "skill_id": skill_id, "binding": saved}


@router.delete("/api/v1/admin/skills/{skill_id}")
async def admin_delete_skill(skill_id: str) -> dict:
    """Delete a skill (its JSON file) and its binding."""
    from . import skills, skill_bindings
    if not skills.delete_skill(skill_id):
        raise HTTPException(404, "skill not found")
    skill_bindings.delete_binding(skill_id)
    from . import audit as _audit
    _audit.append_event(actor="admin", action="skill.delete",
                        resource={"skillId": skill_id}, payload={})
    return {"ok": True, "deleted": skill_id}


@router.post("/api/v1/admin/skills/reload")
async def admin_reload_skills() -> dict:
    """Re-scan data/skills/ and re-read bindings without a restart."""
    from . import skills, skill_bindings
    n = skills.load()
    skill_bindings.load()
    return {"ok": True, "skills_loaded": n}


@router.post("/api/v1/admin/skills/upload")
async def admin_upload_skill(
    mode: str = Form("zip"),
    uploaded_by: str = Form("admin"),
    agents: str = Form(""),                        # optional JSON array of agent ids
    overwrite: bool = Form(False),
    file: UploadFile | None = File(None),          # zip mode
    files: list[UploadFile] = File(default=[]),    # folder mode (paths in .filename)
) -> dict:
    """Validate + install an uploaded skill bundle (.zip or folder).

    MODERATE validation: hard-reject on structure / schema / safety; warn on
    referential gaps; never silently overwrite. Python tool files are NOT
    accepted yet — bundles are data-only (skill.json + optional corpus .jsonl).
    Returns {ok, skill_id, agents, tools, corpus_chunks, errors[], warnings[]}.
    """
    import io as _io
    import json as _json
    import re as _re
    import zipfile as _zip

    errors: list[str] = []
    warnings: list[str] = []
    MAX_FILES = 50
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB (decompressed) — blocks zip bombs / DoS
    ALLOWED_EXT = {".json", ".jsonl", ".md", ".txt"}

    def _safe_relpath(p: str):
        p = (p or "").replace("\\", "/").strip().lstrip("/")
        if not p or ".." in p.split("/") or ":" in p:
            return None      # reject traversal / absolute / drive paths (zip-slip)
        return p

    def _base(rp: str) -> str:
        return rp.rsplit("/", 1)[-1].lower()

    def _ext(rp: str) -> str:
        b = _base(rp)
        return ("." + b.rsplit(".", 1)[-1]) if "." in b else ""

    # ---- 1. Collect the bundle's files into {relpath: bytes} ----
    members: dict[str, bytes] = {}
    total = 0
    if (mode or "zip") == "zip":
        if file is None:
            raise HTTPException(400, "no .zip file uploaded")
        try:
            zf = _zip.ZipFile(_io.BytesIO(await file.read()))
        except Exception:
            raise HTTPException(400, "not a valid .zip file")
        for info in zf.infolist():
            if info.is_dir():
                continue
            rp = _safe_relpath(info.filename)
            if rp is None:
                raise HTTPException(400, f"unsafe path in zip: {info.filename}")
            if len(members) >= MAX_FILES:
                raise HTTPException(400, f"too many files (max {MAX_FILES})")
            total += info.file_size
            if total > MAX_BYTES:
                raise HTTPException(400, "bundle too large (max 5 MB)")
            members[rp] = zf.read(info)
    else:
        for uf in (files or []):
            rp = _safe_relpath(uf.filename)
            if rp is None:
                raise HTTPException(400, f"unsafe path: {uf.filename}")
            if len(members) >= MAX_FILES:
                raise HTTPException(400, f"too many files (max {MAX_FILES})")
            data = await uf.read()
            total += len(data)
            if total > MAX_BYTES:
                raise HTTPException(400, "bundle too large (max 5 MB)")
            members[rp] = data
    if not members:
        raise HTTPException(400, "empty bundle — nothing uploaded")

    # ---- 2. File-type allowlist (reject .py + anything executable) ----
    for rp in members:
        e = _ext(rp)
        if e == ".py":
            raise HTTPException(400, f"Python tool files are not accepted yet: {rp}")
        if e not in ALLOWED_EXT:
            raise HTTPException(400, f"file type not allowed: {rp} "
                                    f"(allowed: {', '.join(sorted(ALLOWED_EXT))})")

    # ---- 3. Find the manifest — skill.json OR SKILL.md (or a lone *.json/*.md) ----
    from . import skills
    manifest = None
    for cand in ("skill.json", "skill.md"):
        hits = [rp for rp in members if _base(rp) == cand]
        if hits:
            manifest = hits[0]
            break
    if manifest is None:
        json_md = [rp for rp in members if _ext(rp) in (".json", ".md")]
        if len(json_md) == 1:
            manifest = json_md[0]
    if manifest is None:
        raise HTTPException(400, "bundle must contain a skill.json or a SKILL.md manifest")
    manifest_is_md = _ext(manifest) == ".md"
    manifest_text = members[manifest].decode("utf-8", errors="replace")
    try:
        sk = (skills.parse_skill_md(manifest_text) if manifest_is_md
              else _json.loads(manifest_text))
    except Exception as e:
        raise HTTPException(400, f"manifest parse failed: {e}")
    if not isinstance(sk, dict):
        raise HTTPException(400, "skill manifest must resolve to an object")

    # ---- 4. Schema (moderate) ----
    sid = str(sk.get("id") or "").strip()
    name = str(sk.get("name") or "").strip()
    instructions = str(sk.get("instructions") or "").strip()
    description = str(sk.get("description") or "").strip()
    corpus_id = str(sk.get("corpus_id") or "").strip()
    tool_ids = sk.get("tool_ids") or []
    if not _re.fullmatch(r"[a-z0-9_-]{2,64}", sid):
        errors.append("id must be a lowercase slug matching ^[a-z0-9_-]{2,64}$")
    if not name:
        errors.append("name is required")
    if not instructions:
        errors.append("instructions are required")
    if not (isinstance(tool_ids, list) and all(isinstance(t, str) for t in tool_ids)):
        errors.append("tool_ids must be a list of strings")
        tool_ids = []
    if len(instructions) > 4000:
        errors.append("instructions too long (max 4000 chars)")
    if len(tool_ids) > 20:
        errors.append("too many tool_ids (max 20)")

    # ---- 5. Safety — scan instructions for prompt-injection ----
    from . import prompt_safety as _ps
    scan = _ps.fence_user_input(instructions)
    if scan.suspicious:
        errors.append("instructions contain prompt-injection patterns ("
                      + ", ".join(scan.injection_hits) + ")")

    # ---- 6. Collision ----
    from . import skills, skill_bindings
    if sid and sid in skills._SKILLS and not overwrite:
        errors.append(f"skill '{sid}' already exists (set overwrite=true to replace)")

    # ---- 7. Referential (warn, don't reject) ----
    from .tools import get_tool
    missing = [t for t in tool_ids if get_tool(t) is None]
    if missing:
        warnings.append("tool_ids not in the registry yet (the skill will show a "
                        "missing-tools warning until available): " + ", ".join(missing))

    wire_agents: list[str] = []
    if agents:
        try:
            wa = _json.loads(agents)
            wire_agents = [str(a) for a in wa if a] if isinstance(wa, list) else []
        except Exception:
            warnings.append("could not parse the agents list — skipping wiring")

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    # ---- 8. Install the skill (preserve the authored format) ----
    try:
        if manifest_is_md:
            skills.save_skill_md(manifest_text)      # persists as data/skills/<id>.md
        else:
            skills.save_skill(
                id=sid, name=name, description=description, instructions=instructions,
                tool_ids=list(tool_ids), corpus_id=corpus_id, default_agents=[],
                source="external", enabled=True,
                outbound=sk.get("outbound") if isinstance(sk.get("outbound"), dict) else None,
            )
    except ValueError as e:   # invalid outbound contract, malformed md, etc.
        return {"ok": False, "errors": [str(e)], "warnings": warnings}

    # ---- 9. Optional corpus ingestion (.jsonl) ----
    corpus_chunks = 0
    corpus_files = [rp for rp in members if _ext(rp) == ".jsonl"]
    if corpus_files and not corpus_id:
        warnings.append("a corpus .jsonl was included but skill.json has no "
                        "corpus_id — corpus not ingested")
    elif corpus_files:
        from .retrieval.pipeline import ingest_jsonl
        for rp in corpus_files:
            try:
                corpus_chunks += len(ingest_jsonl(
                    corpus_id, members[rp].decode("utf-8"), uploaded_by=uploaded_by))
            except Exception as e:  # noqa: BLE001
                warnings.append(f"corpus {rp} ingest failed: {e}")

    # ---- 10. Wire to the selected agents (optional) ----
    if wire_agents:
        skill_bindings.set_binding(sid, enabled=True, agents=wire_agents)

    from . import audit as _audit
    _audit.append_event(
        actor="admin", action="skill.upload", resource={"skill": sid},
        payload={"agents": wire_agents, "tools": list(tool_ids),
                 "corpus_chunks": corpus_chunks, "warnings": warnings})
    skills.load()
    skill_bindings.load()
    return {"ok": True, "skill_id": sid, "agents": wire_agents,
            "tools": list(tool_ids), "corpus_chunks": corpus_chunks,
            "errors": [], "warnings": warnings}


class SkillMdRequest(BaseModel):
    markdown: str
    agents: list[str] = []
    overwrite: bool = True


@router.post("/api/v1/admin/skills/md")
async def admin_save_skill_md(req: SkillMdRequest) -> dict:
    """Create/update a skill from a SKILL.md (YAML frontmatter + markdown body).

    Same MODERATE validation as the upload flow; persists as data/skills/<id>.md.
    Returns {ok, skill_id, agents, errors[], warnings[]}."""
    import re as _re
    from . import skills, skill_bindings, prompt_safety as _ps
    from .tools import get_tool
    try:
        sk = skills.parse_skill_md(req.markdown or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errors": [f"SKILL.md parse failed: {e}"], "warnings": []}

    errors: list[str] = []
    warnings: list[str] = []
    sid = str(sk.get("id") or "").strip()
    name = str(sk.get("name") or "").strip()
    instructions = str(sk.get("instructions") or "").strip()
    tool_ids = sk.get("tool_ids") or []
    if not _re.fullmatch(r"[a-z0-9_-]{2,64}", sid):
        errors.append("frontmatter 'id' must be a lowercase slug ^[a-z0-9_-]{2,64}$")
    if not name:
        errors.append("frontmatter 'name' is required")
    if not instructions:
        errors.append("instructions (the markdown body) are required")
    if not (isinstance(tool_ids, list) and all(isinstance(t, str) for t in tool_ids)):
        errors.append("tool_ids must be a list of strings")
        tool_ids = []
    if len(instructions) > 4000:
        errors.append("instructions too long (max 4000 chars)")
    scan = _ps.fence_user_input(instructions)
    if scan.suspicious:
        errors.append("instructions contain prompt-injection patterns ("
                      + ", ".join(scan.injection_hits) + ")")
    if sid and sid in skills._SKILLS and not req.overwrite:
        errors.append(f"skill '{sid}' already exists (enable overwrite to replace)")
    missing = [t for t in tool_ids if get_tool(t) is None]
    if missing:
        warnings.append("tool_ids not in the registry yet: " + ", ".join(missing))
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    try:
        skills.save_skill_md(req.markdown)
    except ValueError as e:
        return {"ok": False, "errors": [str(e)], "warnings": warnings}
    wired = [str(a) for a in (req.agents or []) if a]
    if wired:
        skill_bindings.set_binding(sid, enabled=True, agents=wired)
    from . import audit as _audit
    _audit.append_event(actor="admin", action="skill.save_md",
                        resource={"skill": sid}, payload={"agents": wired})
    return {"ok": True, "skill_id": sid, "agents": wired,
            "errors": [], "warnings": warnings}



# Real Bulbul v3 voices per Sarvam docs (March 2026).
# https://docs.sarvam.ai/api-reference-docs/text-to-speech/convert
# NOTE: anushka / manisha / vidya / arya are bulbul:v2-only and will 4xx on v3.
BULBUL_VOICES = [
    # Male
    {"id": "shubh",    "gender": "male",   "tone": "warm, default"},
    {"id": "aditya",   "gender": "male",   "tone": "youthful, approachable"},
    {"id": "rahul",    "gender": "male",   "tone": "professional"},
    {"id": "rohan",    "gender": "male",   "tone": "calm, narrator"},
    {"id": "varun",    "gender": "male",   "tone": "energetic"},
    {"id": "amit",     "gender": "male",   "tone": "deep, formal"},
    {"id": "dev",      "gender": "male",   "tone": "friendly"},
    {"id": "kabir",    "gender": "male",   "tone": "mature, authoritative"},
    {"id": "ashutosh", "gender": "male",   "tone": "casual"},
    {"id": "advait",   "gender": "male",   "tone": "soft, calm"},
    {"id": "anand",    "gender": "male",   "tone": "neutral"},
    {"id": "tarun",    "gender": "male",   "tone": "youthful"},
    {"id": "sunny",    "gender": "male",   "tone": "upbeat"},
    {"id": "mani",     "gender": "male",   "tone": "deep"},
    {"id": "vijay",    "gender": "male",   "tone": "confident"},
    {"id": "mohit",    "gender": "male",   "tone": "casual"},
    {"id": "rehan",    "gender": "male",   "tone": "warm"},
    {"id": "soham",    "gender": "male",   "tone": "friendly, calm"},
    # Female
    {"id": "ritu",     "gender": "female", "tone": "approachable"},
    {"id": "priya",    "gender": "female", "tone": "warm, empathetic"},
    {"id": "neha",     "gender": "female", "tone": "youthful, clear"},
    {"id": "pooja",    "gender": "female", "tone": "calm, professional"},
    {"id": "simran",   "gender": "female", "tone": "authoritative"},
    {"id": "kavya",    "gender": "female", "tone": "youthful"},
    {"id": "ishita",   "gender": "female", "tone": "soft, gentle"},
    {"id": "shreya",   "gender": "female", "tone": "expressive"},
    {"id": "roopa",    "gender": "female", "tone": "mature, news-anchor"},
    {"id": "tanya",    "gender": "female", "tone": "confident"},
    {"id": "shruti",   "gender": "female", "tone": "energetic"},
    {"id": "suhani",   "gender": "female", "tone": "friendly"},
    {"id": "kavitha",  "gender": "female", "tone": "warm, mature"},
    {"id": "rupali",   "gender": "female", "tone": "calm"},
]


@router.get("/api/v1/admin/voices")
async def admin_list_voices() -> dict:
    return {
        "voices": BULBUL_VOICES,
        "supported_languages": [
            "hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN",
            "bn-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN", "en-IN",
        ],
    }


@router.post("/api/v1/admin/voices/preview")
async def admin_voice_preview(req: VoicePreviewRequest) -> dict:
    tts = await tts_synthesize(
        req.text, target_language_code=req.language, speaker=req.voice,
    )
    from .orchestrator import _save_audio_blob
    audio_url = await _save_audio_blob(tts.audio_bytes, tts.mime) if tts.audio_bytes else ""

    # Three outcomes:
    #   A. LIVE Bulbul success            → mock=False, error=""
    #   B. LIVE Bulbul failed (bad voice) → mock=True,  error="HTTP 400: ..."
    #   C. No API key (mock mode)         → mock=True,  error=""
    if not tts.mock:
        hint = f"🎤 Real Bulbul v3 voice ({req.voice}) in {req.language}."
        mode = "LIVE_BULBUL"
    elif tts.error:
        hint = (f"⚠️ Sarvam rejected this voice. Sarvam said: {tts.error}\n"
                f"Likely cause: '{req.voice}' is not a valid bulbul:v3 voice. "
                f"Try shubh, aditya, rahul (male) or ritu, priya, simran (female). "
                f"v2 voices like vidya/manisha/anushka/arya do NOT work on v3.")
        mode = "FALLBACK_AFTER_ERROR"
    else:
        hint = ("🔔 You heard a two-tone chime because Sarvam is in mock mode. "
                "Set SARVAM_API_KEY in .env and restart.")
        mode = "MOCK_CHIME"

    return {
        "ok": True, "audio_url": audio_url,
        "is_mock": tts.mock,
        "mode": mode,
        "duration_s": tts.duration_s,
        "voice": req.voice, "language": req.language,
        "error": tts.error or None,
        "http_status": tts.http_status or None,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# BROADCASTS
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/broadcasts")
async def admin_list_broadcasts() -> dict:
    return {
        "broadcasts": [
            {
                "broadcast_id": b.broadcast_id, "agent_id": b.agent_id,
                "title": b.title, "body": b.body,
                "languages": b.languages,
                "bodies_by_language": b.bodies_by_language,
                "target_audience": b.target_audience,
                "status": b.status,
                "composed_by": b.composed_by,
                "composed_at": b.composed_at.isoformat() if b.composed_at else None,
                "approved_by": b.approved_by,
                "approved_at": b.approved_at.isoformat() if b.approved_at else None,
                "sent_count": b.sent_count,
            }
            for b in bcast.list_all()
        ]
    }


@router.post("/api/v1/admin/broadcasts")
async def admin_create_broadcast(req: BroadcastCreate) -> dict:
    if req.agentId not in AGENTS:
        raise HTTPException(404, "unknown agent")
    b = bcast.create(
        agent_id=req.agentId, title=req.title, body=req.body,
        target_audience=req.targetAudience,
        languages=req.languages, composed_by=req.composedBy,
    )
    if req.autoTranslate and req.languages:
        await bcast.translate_into(b.broadcast_id, req.languages)
    return {"ok": True, "broadcast_id": b.broadcast_id, "status": b.status}


@router.post("/api/v1/admin/broadcasts/{bid}/approve")
async def admin_approve_broadcast(bid: str, req: ApproveRequest) -> dict:
    b = bcast.approve(bid, req.by)
    if not b:
        raise HTTPException(400, "could not approve (not found OR four-eyes violation)")
    return {"ok": True, "status": b.status, "approved_by": b.approved_by}


@router.post("/api/v1/admin/broadcasts/{bid}/reject")
async def admin_reject_broadcast(bid: str, req: ApproveRequest) -> dict:
    b = bcast.reject(bid, req.by)
    if not b:
        raise HTTPException(404, "broadcast not found")
    return {"ok": True, "status": b.status}


@router.post("/api/v1/admin/broadcasts/{bid}/send")
async def admin_send_broadcast(bid: str) -> dict:
    n = await bcast.send(bid)
    return {"ok": True, "sent_count": n}


# ---------------------------------------------------------------------------
# COORDINATOR introspection
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/coordinator/recipes")
async def admin_coord_recipes() -> dict:
    return {"recipes": coord.list_recipes()}


@router.get("/api/v1/admin/coordinator/sessions")
async def admin_coord_sessions() -> dict:
    out = []
    for sid, s in coord._SESSIONS.items():
        out.append({
            "sessionId": s.session_id, "citizenId": s.citizen_id,
            "recipeId": s.recipe_id,
            "currentIdx": s.current_step_idx, "state": s.state,
            "completed": s.completed,
            "started_at": s.started_at.isoformat(),
            "history_len": len(s.history),
        })
    return {"sessions": out}


# ---------------------------------------------------------------------------
# SANDBOX — quick agent test without affecting real conversations
# ---------------------------------------------------------------------------

@router.post("/api/v1/admin/agents/{agent_id}/sandbox")
async def admin_sandbox(agent_id: str, req: SandboxMessage) -> dict:
    agent = get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    # Direct LLM call — no persistence, no consent, no tools
    from .llm import llm
    from .rag import retrieve
    chunks = retrieve(agent.corpus_id or agent.id, req.text, k=3)
    rag_context = "\n\n".join(c.to_context_block() for c in chunks)
    msgs = [
        {"role": "system", "content": agent.system_prompt(rag_context=rag_context)},
        {"role": "user", "content": req.text},
    ]
    reply = await llm.chat_complete(messages=msgs, temperature=0.4, max_tokens=600)
    return {"ok": True, "reply": reply, "rag_chunks": len(chunks)}


# ---------------------------------------------------------------------------
# METRICS — simple counters
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/metrics")
async def admin_metrics() -> dict:
    total_msgs = sum(len(msgs) for msgs in store.conversations.values())
    citizens = len(store.citizens)
    convs = len(store.conv_meta)
    return {
        "citizens": citizens,
        "conversations": convs,
        "messages": total_msgs,
        "agents": len(AGENTS),
        "tools": len(all_tools()),
        "broadcasts": len(bcast.list_all()),
        "coordinator_sessions": len(coord._SESSIONS),
    }


# ---------------------------------------------------------------------------
# Phase 6h — LATENCY metrics (per-turn stage breakdown for the dashboard)
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/metrics/latency")
async def admin_latency(window: int = 60, recent_n: int = 30) -> dict:
    """Per-stage latency aggregates + recent turns.

    `window` is the look-back in minutes (default last hour).
    Stages: stt, rag, tool, llm_first, llm_total, post, tts, total (ms).
    """
    from . import latency_metrics as _lat
    window = max(5, min(window, 7 * 24 * 60))
    return {
        "summary": _lat.summary(window_minutes=window),
        "recent": _lat.recent(max(5, min(recent_n, 200))),
    }
