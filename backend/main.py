"""FastAPI application — Phase 3.

Phase 3 adds:
  - Twilio webhook routes (mounted from routes_twilio.py)
  - Channel-aware orchestrator (same handle_citizen_message function;
    new `channel` argument routed by the channel_dispatcher)
  - PUBLIC_BASE_URL config so audio replies can be served back to Twilio
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

# Configure logging so the Twilio client and webhook logs are actually visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import (
    FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Depends
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agents import AGENTS, all_agents, get_agent
from .config import settings
from . import auth as _auth
from .consent import decide as consent_decide, get_request as get_consent_request
from .models import (
    AgentListResponse, AgentMeta,
    AuthInitRequest, AuthInitResponse,
    ConsentDecisionRequest,
    ConversationHistoryResponse,
    SendMessageRequest, SendMessageResponse,
)
from .orchestrator import (
    handle_citizen_message, handle_citizen_voice,
    resume_after_consent_decision,
)
from .admin_storage import load_into_registry as _load_agents_from_storage
from . import audit as _audit
from .calls import livekit_configured
from . import consent as _consent
from . import crypto_utils as _crypto
from .pii_redaction import install_global_log_redaction
from .rag import corpus_stats, load_corpora
from .routes_admin import router as admin_router
from .routes_calls import router as calls_router
from .routes_corpus import router as corpus_router
from .routes_dsr import router as dsr_router
from .routes_media import router as media_router
from .routes_llm import router as llm_router
from .routes_sarvam_diag import router as sarvam_diag_router
from .routes_twilio import router as twilio_router
# Phase 6e — records / schemes / projects + admin extensions
from .routes_records import router as records_router
from .routes_schemes import router as schemes_router
from .routes_projects import router as projects_router
from .routes_e6_admin import router as e6_admin_router
# Phase 6g — internal tool execution for the LiveKit voice worker
from .routes_internal import router as internal_router
# Callback Agent Platform — outbound skill-driven calling
from .records import sla as _records_sla
from .records import sweeper as _records_sweeper
from . import schemes as _schemes
from . import projects as _projects
from . import workflows as _workflows
from . import personas as _personas
from .sarvam_client import sarvam
from .store import store
from .tools import all_tools
from . import tool_loader as _tool_loader
from . import tool_bindings as _tool_bindings
from . import mcp_loader as _mcp_loader
from . import skills as _skills
from . import skill_bindings as _skill_bindings
from .ws_manager import ws_manager


_demo_task = None
_root_task = None
_sweeper_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.enforce_production_ready()
    # Phase 6 init order matters:
    #   1. PII redaction filter on all loggers (so subsequent INFO lines are clean)
    #   2. Crypto keys (signs ledger + audit entries)
    #   3. Audit log (replay to recover last hash)
    #   4. Consent ledger (replay to rebuild active grants)
    #   5. Corpora + agents
    install_global_log_redaction()
    _crypto.init_keys()
    _audit.init_audit()
    _consent.init_ledger()
    load_corpora()
    _load_agents_from_storage()    # Phase 5: hot-load agents from data/agents.json
    # Phase 6c — copy seed personas into the writable data dir, then load
    _seed_personas_into_data_dir()
    _personas.load_examples()
    _personas.load_voice_examples()   # Phase 6f — spoken-style voice example bank
    # Phase 6e — records casework layer: SLA/escalation matrix, scheme catalog,
    # project feed, workflow templates (cross-agent recipes register into the
    # coordinator). Each loader is isolated so a single failure (e.g. a
    # read-only data dir or a malformed JSON file on one machine) degrades
    # that feature instead of crashing the whole server at startup.
    for _name, _fn in (("sla", _records_sla.load), ("schemes", _schemes.load),
                       ("projects", _projects.load), ("workflows", _workflows.load)):
        try:
            _fn()
        except Exception as _e:
            logging.getLogger("startup").error(
                "Phase 6e loader '%s' failed (continuing without it): %s", _name, _e)

    # Configurable tools & MCP — three sources feed the one registry:
    #   1. built-in tools (already registered at import of backend.tools)
    #   2. drop-in Python plugins under backend/tool_plugins/
    #   3. external MCP servers listed in data/mcp_servers.json
    # then the operator's bindings (data/tool_bindings.json) control on/off +
    # agent wiring at runtime. Each loader is isolated so one failure degrades
    # that source without breaking startup or the other tools.
    for _name, _fn in (("tool_plugins", _tool_loader.load),
                       ("tool_bindings", _tool_bindings.load),
                       ("skills", _skills.load),
                       ("skill_bindings", _skill_bindings.load)):
        try:
            _fn()
        except Exception as _e:
            logging.getLogger("startup").error(
                "Tool loader '%s' failed (continuing without it): %s", _name, _e)
    # MCP needs the running event loop to connect to external servers, so it
    # runs here (awaited) rather than in the sync loop above. An unreachable
    # server degrades to "its tools are temporarily unavailable" — it never
    # blocks startup or the other tools.
    try:
        _mcp_loader.load()              # read data/mcp_servers.json
        await _mcp_loader.connect_all() # connect + register wrapper tools
    except Exception as _e:
        logging.getLogger("startup").error(
            "MCP loader failed (continuing without it): %s", _e)
    stats = corpus_stats()
    # Force-build the LLM provider so we can show its info in the banner
    from .llm import get_llm
    llm_info = get_llm().info()
    sovereign_badge = "🇮🇳 sovereign" if llm_info.is_sovereign else "⚠️  overseas"
    print("=" * 60)
    print("  Government Services Multi-Agent Backend — PHASE 5b")
    print("=" * 60)
    # Sarvam specifically — what the user is here for
    sarvam_key_ok = bool(settings.sarvam_api_key)
    print(f"  Sarvam API key:    {'✓ present' if sarvam_key_ok else '✗ MISSING (chat/STT/TTS/Vision will MOCK)'}")
    print(f"  Sarvam base URL:   {settings.sarvam_base_url}")
    print(f"  Sarvam chat:       {settings.sarvam_chat_model}  ({'LIVE' if sarvam_key_ok else 'MOCK'})")
    print(f"  Sarvam STT/TTS:    saaras:v3 / bulbul:v3  ({'LIVE' if sarvam_key_ok else 'MOCK (chime)'})")
    vision_label = "JOB API configured" if sarvam_key_ok else "MOCK (fixtures)"
    if sarvam_key_ok and not settings.allow_mock_providers:
        vision_label = "JOB API configured; inline fixture OCR disabled"
    print(f"  Sarvam Vision:     {vision_label}")
    print(f"  LLM provider:      {llm_info.display_name}  [{sovereign_badge}]")
    print(f"  Twilio mode:       {'MOCK' if settings.twilio_mock_mode else 'LIVE'}")
    print(f"  LiveKit mode:      {'LIVE' if livekit_configured() else 'MOCK (press-to-talk)'}")
    if not sarvam_key_ok:
        print()
        print("  ⚠️  Sarvam in MOCK mode. Voice previews will play a 2-tone CHIME, not Bulbul.")
        print("     Set SARVAM_API_KEY in .env and restart for real voices.")
        print("     Diagnostics:  GET /api/v1/admin/sarvam/diagnose  or  python -m backend.sarvam_diagnostics")
    if not settings.twilio_mock_mode:
        print(f"    SID:         {settings.twilio_account_sid[:8]}…")
        print(f"    From WA:     {settings.twilio_whatsapp_from}")
        print(f"    Validate sig:{settings.twilio_validate_signatures}")
        print(f"    Public URL:  {settings.public_base_url or '(not set — audio replies disabled)'}")
    print(f"  Agents:      {len(AGENTS)}  ({', '.join(AGENTS.keys())})")
    print(f"  Corpora:     {sum(stats.values())} chunks")
    print(f"  Tools:       {len(all_tools())}")
    print(f"  Open:        http://{settings.host}:{settings.port}/")
    print(f"  Twilio webhook URL (configure in Twilio Console):")
    base = settings.public_base_url or f"http://{settings.host}:{settings.port}"
    print(f"               {base}/webhooks/twilio/whatsapp")
    print(f"               {base}/webhooks/twilio/voice")
    print("=" * 60)

    if settings.push_demo_enabled:
        global _demo_task
        _demo_task = asyncio.create_task(_broadcast_demo_loop())

    # Phase 6: daily Merkle root for the audit log
    global _root_task
    _root_task = asyncio.create_task(_audit.daily_root_loop())

    # Phase 6e — SLA escalation sweeper (auto-escalates L1→L4 on breach).
    # Wrapped so a sweeper/banner failure can never prevent the server from
    # binding + serving.
    try:
        global _sweeper_task
        _sweeper_task = asyncio.create_task(_records_sweeper.sweep_loop())
        from .records.store import records_store as _rs
        from . import auth as _auth
        print(f"  Phase 6e:    records={len(_rs.all())}  schemes={len(_schemes.all_schemes())}  "
              f"projects={len(_projects.all_projects())}  workflows={len(_workflows.all_templates())}  "
              f"sla_demo_clock={_records_sla.demo_clock()}")
        print(f"  Phase 6e sec: require_auth={_auth.require_auth_enabled()}  "
              f"admin_gate={'ON' if _auth.admin_gate_configured() else 'OFF (dev — set ADMIN_API_TOKEN)'}  "
              f"record_ownership_checks=ON  ui_xss_escaping=ON")
        if not _auth.admin_gate_configured():
            print("  ⚠️  Admin API is OPEN (no ADMIN_API_TOKEN). Set it before any shared/prod deploy.")
        print("=" * 60)
    except Exception as _e:
        logging.getLogger("startup").error("Phase 6e startup extras failed (continuing): %s", _e)

    # Print the Phase 6 banner extension AFTER everything is up
    crypto_info = _crypto.backend_info()
    print(f"  Phase 6:     crypto={crypto_info['algorithm']}  "
          f"consent_ledger=on  audit_log=on  pii_redaction=on  dsr_endpoints=on")
    # Phase 6b — count agents with a pinned LLM provider
    _pinned = [a for a in AGENTS.values() if getattr(a, "llm_provider", None)]
    print(f"  Phase 6b:    prompt_safety=on  output_leakage_scan=on  "
          f"per_agent_llm={len(_pinned)}/{len(AGENTS)}  my_data_ui=on")
    # Phase 6c — corpus + persona stats
    _ex_stats = _personas.stats()
    _personas_total = sum(_ex_stats.values())
    _structured_chunks = sum(corpus_stats().values())
    _chunks_per_agent = ", ".join(f"{k}={v}" for k, v in corpus_stats().items()) or "(none)"
    print(f"  Phase 6c:    structured_rag=on  upload_api=on  "
          f"few_shot_examples={_personas_total}  chunks={_structured_chunks}")
    print(f"               chunks_per_agent: {_chunks_per_agent}")
    print("=" * 60)

    yield

    if _demo_task:
        _demo_task.cancel()
    if _root_task:
        _root_task.cancel()
    if _sweeper_task:
        _sweeper_task.cancel()
    try:
        await _mcp_loader.aclose()   # close pooled MCP connections
    except Exception:
        pass
    await sarvam.close()


def _seed_personas_into_data_dir() -> None:
    """Phase 6c — copy shipped persona example files into the writable
    data dir on first start so the admin can edit them via the UI.

    If a file with the same name already exists in data/personas, we DON'T
    overwrite — the admin's edits win.
    """
    src = Path(__file__).resolve().parent.parent / "data" / "personas"
    dst = Path(settings.data_dir) / "personas"
    if not src.exists() or src == dst:
        return
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    for f in src.glob("*_examples.jsonl"):
        target = dst / f.name
        if target.exists():
            continue
        try:
            target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            print(f"  (persona seed) skipped {f.name}: {e}")

    # Phase 6f — also seed the spoken-style voice example bank.
    vsrc = src / "voice"
    vdst = dst / "voice"
    if vsrc.exists() and vsrc != vdst:
        try:
            vdst.mkdir(parents=True, exist_ok=True)
            for f in vsrc.glob("*_examples.jsonl"):
                target = vdst / f.name
                if target.exists():
                    continue
                target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            print(f"  (voice persona seed) skipped: {e}")


async def _broadcast_demo_loop() -> None:
    await asyncio.sleep(25)
    while True:
        try:
            agent = random.choice(list(AGENTS.values()))
            body = random.choice(agent.push_pool).replace("{n}", str(random.randint(1000, 9999)))
            await ws_manager.broadcast({
                "type": "broadcast", "agentId": agent.id,
                "title": agent.name, "body": body, "convId": None,
            })
            await asyncio.sleep(30 + random.random() * 30)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(30)


app = FastAPI(title="Government Services Multi-Agent Backend",
              version="0.3.0-phase3", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Mount routers
app.include_router(twilio_router)

# --- Sovereign AI Substrate (NSDC Part B PoC) — opt-in via SUBSTRATE_RAG=true
import os as _os
if _os.getenv("SUBSTRATE_RAG", "").lower() in ("1", "true", "yes"):
    from .routes_substrate import router as substrate_router, ui_router as substrate_ui_router
    app.include_router(substrate_router)
    app.include_router(substrate_ui_router)
    print("  Substrate RAG:     ENABLED (/api/v1/substrate/*)")
app.include_router(media_router)
app.include_router(calls_router)
app.include_router(llm_router)
app.include_router(admin_router, dependencies=[Depends(_auth.require_admin)])
app.include_router(sarvam_diag_router, dependencies=[Depends(_auth.require_admin)])
app.include_router(dsr_router)
app.include_router(corpus_router, dependencies=[Depends(_auth.require_admin)])    # Phase 6c — RAG corpus + personas mgmt
# Phase 6e — casework, schemes, projects, admin extensions
app.include_router(records_router)
app.include_router(schemes_router)
app.include_router(projects_router)
app.include_router(e6_admin_router, dependencies=[Depends(_auth.require_admin)])
app.include_router(internal_router)   # Phase 6g — voice-worker tool bridge


# ---------------------------------------------------------------------------
# Health & dashboards
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    stats = corpus_stats()
    from .llm import get_llm
    llm_info = get_llm().info()
    return {
        "status": "ok", "phase": "4b",
        "environment": settings.app_env,
        "productionMode": settings.is_production,
        "productionIssues": settings.production_issues(),
        "mockProvidersAllowed": settings.allow_mock_providers,
        "demoRoutesAllowed": settings.allow_demo_routes,
        "llm": llm_info.as_dict(),
        "mode": "mock" if settings.mock_mode else "live",
        "twilio_mode": "mock" if settings.twilio_mock_mode else "live",
        "twilio_validate_signatures": settings.twilio_validate_signatures,
        "livekit_mode": "live" if livekit_configured() else "mock",
        "vision_mode": ("job_api_configured" if not settings.mock_mode else "mock"),
        "public_base_url": settings.public_base_url,
        "agents": len(AGENTS),
        "tools": len(all_tools()),
        "corpus_chunks": sum(stats.values()),
        "wsConnections": ws_manager.connected_count,
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/v1/auth/init", response_model=AuthInitResponse)
async def auth_init(req: AuthInitRequest) -> AuthInitResponse:
    msisdn = req.msisdn.strip().lstrip("+").replace(" ", "")
    if not msisdn.isdigit() or len(msisdn) < 10:
        raise HTTPException(400, "msisdn must be at least 10 digits")
    msisdn = msisdn[-10:]
    cid = store.get_or_create_citizen(msisdn)

    # Phase 6d — resolve the citizen's state.
    # 1. Explicit state_code from the simulator wins
    # 2. Otherwise auto-detect from the MSISDN's first 4 digits
    # 3. Otherwise leave unset — orchestrator will fall back to DEFAULT_STATE_CODE
    from .states import (get_state, detect_state_from_msisdn,
                         DEFAULT_STATE_CODE)
    state_obj = None
    auto_detected = False
    if req.state_code:
        state_obj = get_state(req.state_code)
    if not state_obj:
        state_obj = detect_state_from_msisdn(msisdn)
        if state_obj:
            auto_detected = True
    if state_obj:
        store.set_citizen_state(cid, state_obj.code, state_obj.primary_language)

    # Phase 6e — issue a SIGNED session token (was a random, never-checked
    # string). Clients send it as `Authorization: Bearer <token>`; the WS
    # endpoint and record endpoints verify it when REQUIRE_AUTH is on.
    from . import auth as _auth
    return AuthInitResponse(
        citizenId=cid, msisdn=msisdn,
        wsToken=_auth.mint_session(cid),
        stateCode=state_obj.code if state_obj else None,
        stateName=state_obj.name if state_obj else None,
        stateEmoji=state_obj.emoji if state_obj else None,
        primaryLanguage=state_obj.primary_language if state_obj else None,
        stateAutoDetected=auto_detected,
    )


@app.get("/api/v1/states")
async def list_states_endpoint() -> dict:
    """Return the full registry of states + UTs so the simulator can render
    a state-picker dropdown."""
    from .states import states_list_json
    return {"states": states_list_json()}


from pydantic import BaseModel as _BaseModel


class StateChangeRequest(_BaseModel):
    state_code: str


@app.post("/api/v1/citizens/{citizen_id}/state")
async def set_citizen_state(citizen_id: str, req: StateChangeRequest) -> dict:
    """Citizen-driven override when the auto-detected state was wrong."""
    from .states import get_state
    s = get_state(req.state_code)
    if not s:
        raise HTTPException(400, f"unknown state code {req.state_code}")
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")
    store.set_citizen_state(citizen_id, s.code, s.primary_language)
    return {"ok": True, "stateCode": s.code, "stateName": s.name,
            "primaryLanguage": s.primary_language, "emoji": s.emoji}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@app.get("/api/v1/agents", response_model=AgentListResponse)
async def list_agents() -> AgentListResponse:
    """Phase 6d — citizen-facing endpoint, returns ONLY enabled agents.
    Disabled agents are managed via the admin console at /api/v1/admin/agents."""
    return AgentListResponse(agents=[
        AgentMeta(
            id=a.id, name=a.name, emoji=a.emoji, color=a.color, bg=a.bg,
            description=a.description, pinned=a.pinned, voice=a.voice,
            tools=a.tool_ids,
            languages=["ta-IN", "hi-IN", "en-IN", "te-IN", "kn-IN", "ml-IN",
                       "mr-IN", "bn-IN", "gu-IN", "pa-IN", "od-IN", "as-IN",
                       "ur-IN", "ne-IN", "kok-IN", "ks-IN", "sd-IN", "sa-IN",
                       "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN"],
        )
        for a in all_agents()
        if getattr(a, "enabled", True)
    ])


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@app.get("/api/v1/citizens/{citizen_id}/conversations")
async def list_conversations(citizen_id: str) -> dict:
    citizen = store.get_citizen(citizen_id)
    if not citizen:
        raise HTTPException(404, "citizen not found")
    return {
        "citizenId": citizen_id,
        "language": citizen.get("language", "en-IN"),
        "previews": store.last_previews(citizen_id),
    }


@app.get(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/messages",
    response_model=ConversationHistoryResponse,
)
async def conv_history(citizen_id: str, agent_id: str) -> ConversationHistoryResponse:
    if not get_agent(agent_id):
        raise HTTPException(404, "agent not found")
    conv_id = store.get_or_create_conv(citizen_id, agent_id)
    return ConversationHistoryResponse(
        convId=conv_id, agentId=agent_id, messages=store.history(conv_id, limit=100)
    )


def _require_enabled_agent(agent_id: str):
    """Phase 6d — common guard for citizen-facing endpoints.

    Returns the agent if it exists AND is enabled. Otherwise raises:
      - 404 if no such agent
      - 503 with a friendly message if temporarily disabled (so the
        simulator can show a maintenance notice instead of a hard error)
    """
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if not getattr(a, "enabled", True):
        raise HTTPException(
            503,
            f"{a.name} is temporarily offline for maintenance. "
            f"Please try again in a few minutes."
        )
    return a


@app.post(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/messages",
    response_model=SendMessageResponse,
)
async def send_message(citizen_id: str, agent_id: str, req: SendMessageRequest) -> SendMessageResponse:
    _require_enabled_agent(agent_id)
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text required")
    msg = await handle_citizen_message(
        citizen_id=citizen_id, agent_id=agent_id,
        text=req.text.strip(), client_msg_id=req.clientMsgId,
        channel="simulator",
    )
    return SendMessageResponse(accepted=True, serverMsgId=msg.id, convId=msg.convId)


@app.post(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/voice",
    response_model=SendMessageResponse,
)
async def send_voice(
    citizen_id: str, agent_id: str,
    audio: UploadFile = File(...),
    clientMsgId: str | None = Form(default=None),
) -> SendMessageResponse:
    _require_enabled_agent(agent_id)
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "audio file is empty")
    msg = await handle_citizen_voice(
        citizen_id=citizen_id, agent_id=agent_id,
        audio_bytes=audio_bytes, mime_type=audio.content_type or "audio/webm",
        client_msg_id=clientMsgId, channel="simulator",
    )
    return SendMessageResponse(accepted=True, serverMsgId=msg.id, convId=msg.convId)


# ---------------------------------------------------------------------------
# Audio serving
# ---------------------------------------------------------------------------

@app.get("/api/v1/audio/{name}")
async def serve_audio(name: str) -> FileResponse:
    safe = os.path.basename(name)
    p = Path(settings.data_dir) / "audio" / safe
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "audio not found")
    return FileResponse(str(p))


@app.get("/api/v1/uploads/{name}")
async def serve_upload(name: str) -> FileResponse:
    safe = os.path.basename(name)
    p = Path(settings.data_dir) / "uploads" / safe
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "upload not found")
    return FileResponse(str(p))


# ---------------------------------------------------------------------------
# Consent decision
# ---------------------------------------------------------------------------

@app.post("/api/v1/consent/{request_id}/decide")
async def decide_consent(
    request_id: str, body: ConsentDecisionRequest, citizenId: str,
) -> dict:
    req = get_consent_request(request_id)
    if not req or req.citizen_id != citizenId:
        raise HTTPException(404, "consent request not found")
    decided = consent_decide(request_id, citizenId, body.decision)
    if not decided:
        raise HTTPException(400, "could not decide")
    conv_id = store.conv_id(citizenId, req.agent_id)
    asyncio.create_task(resume_after_consent_decision(
        citizen_id=citizenId, agent_id=req.agent_id, conv_id=conv_id,
        tool_id=req.tool_id, decision=decided.status,
        user_text="(resumed after consent)",
        channel="simulator",
    ))
    return {"ok": True, "status": decided.status,
            "grantId": decided.grant_id, "decidedAt": decided.decided_at}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, citizenId: str | None = None,
                     token: str | None = None) -> None:
    if not citizenId or not store.get_citizen(citizenId):
        await ws.accept()
        await ws.send_json({"type": "error", "error": "unknown citizenId"})
        await ws.close()
        return
    # Phase 6e — when REQUIRE_AUTH is on, the socket must present a session
    # token (?token=) that resolves to this citizen. Off by default so the
    # existing simulator keeps connecting.
    from . import auth as _auth
    if _auth.require_auth_enabled():
        if _auth.verify_session(token or "") != citizenId:
            await ws.accept()
            await ws.send_json({"type": "error", "error": "invalid session token"})
            await ws.close()
            return
    await ws_manager.connect(citizenId, ws)
    try:
        citizen = store.get_citizen(citizenId) or {}
        await ws.send_json({
            "type": "state_snapshot",
            "citizenId": citizenId, "msisdn": citizen.get("msisdn"),
            "language": citizen.get("language", "en-IN"),
            "previews": store.last_previews(citizenId),
        })
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(citizenId, ws)


# ---------------------------------------------------------------------------
# Static — simulator at /
# ---------------------------------------------------------------------------

SIMULATOR_DIR = Path(__file__).resolve().parent.parent / "simulator"
ADMIN_DIR     = Path(__file__).resolve().parent.parent / "admin"

if SIMULATOR_DIR.exists():
    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(SIMULATOR_DIR / "index.html")

    @app.get("/services")
    async def services_page() -> FileResponse:
        # Phase 6e — citizen services portal (My Records / Track / Schemes / Projects)
        return FileResponse(SIMULATOR_DIR / "services.html")

    app.mount("/static", StaticFiles(directory=str(SIMULATOR_DIR)), name="static")

if ADMIN_DIR.exists():
    @app.get("/admin/")
    async def admin_root() -> FileResponse:
        return FileResponse(ADMIN_DIR / "index.html")

    @app.get("/admin")
    async def admin_redirect() -> FileResponse:
        return FileResponse(ADMIN_DIR / "index.html")

    @app.get("/admin/ops")
    async def admin_ops() -> FileResponse:
        # Phase 6e — casework operations console
        return FileResponse(ADMIN_DIR / "ops.html")

    app.mount("/admin-static", StaticFiles(directory=str(ADMIN_DIR)), name="admin_static")
