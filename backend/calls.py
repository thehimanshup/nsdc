"""Voice-call session management.

Two modes:

  LIVE mode (LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET set):
    - Mint a LiveKit room + access token
    - Browser uses livekit-client to join via WebRTC
    - A separate Python LiveKit Agent worker process joins the same room
      and runs the Sarvam STT → LLM → TTS pipeline
    - See LIVEKIT_SETUP.md for the worker setup

  MOCK mode (no LiveKit env vars):
    - "Press-to-talk" call simulation
    - Each press-and-hold turn is a Saaras transcription + Bulbul reply,
      back-to-back, with a call-screen UI
    - All audio still round-trips through the orchestrator, so RAG,
      consent flows, and tools all work inside calls too
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .config import settings

log = logging.getLogger("calls")


@dataclass
class CallSession:
    call_id: str
    citizen_id: str
    agent_id: str
    room_name: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    duration_s: float = 0.0
    mode: str = "mock"     # mock | livekit
    transcript_messages: list[str] = field(default_factory=list)


_CALLS: dict[str, CallSession] = {}


def create_call(citizen_id: str, agent_id: str) -> CallSession:
    call_id = f"call_{uuid.uuid4().hex[:12]}"
    room = f"call-{call_id}"
    session = CallSession(
        call_id=call_id, citizen_id=citizen_id,
        agent_id=agent_id, room_name=room,
        mode="livekit" if livekit_configured() else "mock",
    )
    _CALLS[call_id] = session
    return session


def end_call(call_id: str) -> Optional[CallSession]:
    s = _CALLS.get(call_id)
    if not s:
        return None
    s.ended_at = datetime.utcnow()
    s.duration_s = (s.ended_at - s.started_at).total_seconds()
    return s


def get_call(call_id: str) -> Optional[CallSession]:
    return _CALLS.get(call_id)


def append_transcript(call_id: str, line: str) -> None:
    s = _CALLS.get(call_id)
    if s:
        s.transcript_messages.append(line)


# ---------------------------------------------------------------------------
# LiveKit configuration check + token minting
# ---------------------------------------------------------------------------

def livekit_configured() -> bool:
    return bool(
        getattr(settings, "livekit_url", "")
        and getattr(settings, "livekit_api_key", "")
        and getattr(settings, "livekit_api_secret", "")
    )


def mint_livekit_token(*, room: str, identity: str, name: str | None = None,
                       ttl_seconds: int = 3600, metadata: str = "") -> str:
    """Mint a LiveKit access token for the browser participant.

    We construct the JWT inline to avoid a hard dep on the `livekit-api`
    package when not in LIVE mode. The grants we set:
      - room: <room>
      - roomJoin: true
      - canPublish: true (mic)
      - canPublishData: true
      - canSubscribe: true
      - metadata: JSON the agent worker reads to know which department agent to
        embody and the citizen's language/state (Phase 6f — previously the
        worker had no metadata, so every call defaulted to the CMO agent).
    """
    if not livekit_configured():
        return ""
    import base64, hmac, hashlib, json, time

    now = int(time.time())
    payload = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "iat": now,
        "exp": now + ttl_seconds,
        "nbf": now - 10,
        "name": name or identity,
        "metadata": metadata or "",
        "video": {
            "room": room, "roomJoin": True,
            "canPublish": True, "canPublishData": True,
            "canSubscribe": True,
        },
    }

    def _b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body   = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (header + "." + body).encode()
    secret = settings.livekit_api_secret.encode()
    sig = _b64u(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"
