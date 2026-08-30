"""Voice-call routes.

POST /api/v1/calls
    Start a call session. Returns LiveKit room + access token in LIVE
    mode, or a mock-call descriptor in MOCK mode.

DELETE /api/v1/calls/{call_id}
    End a call. Persists the transcript as a system message in the
    conversation, returns duration + transcript.

GET /api/v1/calls/{call_id}
    Inspect an active or recently-ended call.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .agents import get_agent
from .calls import (
    CallSession, create_call, end_call, get_call,
    livekit_configured, mint_livekit_token,
)
from .config import settings
from .models import Message
from .store import store
from .ws_manager import ws_manager

log = logging.getLogger("calls.routes")

router = APIRouter()


class StartCallRequest(BaseModel):
    citizenId: str
    agentId: str


class EndCallRequest(BaseModel):
    callId: str


@router.post("/api/v1/calls")
async def start_call(req: StartCallRequest) -> dict:
    if not get_agent(req.agentId):
        raise HTTPException(404, "agent not found")
    if not store.get_citizen(req.citizenId):
        raise HTTPException(404, "citizen not found")
    if settings.is_production and not livekit_configured():
        raise HTTPException(503, "live call support is not configured")

    s = create_call(req.citizenId, req.agentId)

    response = {
        "callId": s.call_id,
        "agentId": s.agent_id,
        "roomName": s.room_name,
        "mode": s.mode,                     # "livekit" or "mock"
        "startedAt": s.started_at.isoformat(),
    }

    if s.mode == "livekit":
        identity = f"citizen-{req.citizenId}"
        _c = store.get_citizen(req.citizenId) or {}
        # Phase 6f — tell the agent worker WHICH department to embody and the
        # citizen's language/state, via the participant token metadata. Without
        # this the worker defaulted to the CMO agent for every call.
        import json as _json
        _msisdn = _c.get("msisdn", "")
        call_meta = _json.dumps({
            "agent_id": s.agent_id,
            "citizen_msisdn": _msisdn[-4:] if _msisdn else "",
            "language": _c.get("language", "en-IN"),
            "state_code": _c.get("state_code", ""),
        })
        token = mint_livekit_token(
            room=s.room_name, identity=identity,
            name=_msisdn or identity,
            metadata=call_meta,
        )
        response.update({
            "livekitUrl": settings.livekit_url,
            "token": token,
            "note": ("LiveKit configured. Browser should connect to livekitUrl "
                     "with this token. Make sure the LiveKit agent worker is "
                     "running (see LIVEKIT_SETUP.md)."),
        })
    else:
        response["note"] = (
            "MOCK call mode. Browser uses press-to-talk audio uploads to "
            "/api/v1/citizens/{cid}/conversations/{aid}/call-voice with callId. "
            "Each turn runs Saaras + Sarvam-30B + Bulbul (or their mocks)."
        )

    # Push a 'call_started' frame so any open simulator can render the
    # call UI even if the citizen initiates the call from elsewhere.
    await ws_manager.send_to_citizen(req.citizenId, {
        "type": "call_started",
        "callId": s.call_id, "agentId": s.agent_id,
        "mode": s.mode, "roomName": s.room_name,
    })

    return response


@router.delete("/api/v1/calls/{call_id}")
async def end_call_route(call_id: str) -> dict:
    s = end_call(call_id)
    if not s:
        raise HTTPException(404, "call not found")

    # Persist a system message with the call summary
    conv_id = store.get_or_create_conv(s.citizen_id, s.agent_id)
    summary_lines = [f"📞 Voice call ended ({s.mode})",
                     f"   duration: {int(s.duration_s)}s",
                     f"   transcript: {len(s.transcript_messages)} turns"]
    for line in s.transcript_messages[-8:]:
        summary_lines.append(f"   • {line[:120]}")
    msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="system", type="system_event",
        text="\n".join(summary_lines),
        timestamp=datetime.utcnow(), channel="system",
        extra={"callId": s.call_id, "duration_s": s.duration_s},
    )
    store.append(msg)

    await ws_manager.send_to_citizen(s.citizen_id, {
        "type": "call_ended",
        "callId": s.call_id, "agentId": s.agent_id,
        "durationSec": s.duration_s,
        "transcript": s.transcript_messages,
        "summaryMessage": msg.model_dump(mode="json"),
    })

    return {
        "ok": True, "callId": s.call_id,
        "durationSec": s.duration_s,
        "turns": len(s.transcript_messages),
        "mode": s.mode,
    }


@router.get("/api/v1/calls/{call_id}")
async def get_call_route(call_id: str) -> dict:
    s = get_call(call_id)
    if not s:
        raise HTTPException(404, "call not found")
    return {
        "callId": s.call_id, "agentId": s.agent_id,
        "citizenId": s.citizen_id, "mode": s.mode,
        "startedAt": s.started_at.isoformat(),
        "endedAt": s.ended_at.isoformat() if s.ended_at else None,
        "durationSec": s.duration_s,
        "transcript": s.transcript_messages,
    }
