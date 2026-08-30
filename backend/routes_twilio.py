"""Twilio webhook routes.

Endpoints:
  POST /webhooks/twilio/whatsapp     Inbound WhatsApp message from Twilio
  POST /webhooks/twilio/status        Outbound message delivery status callback
  POST /webhooks/twilio/voice         Inbound voice call (Phase 4 will replace
                                       this with a LiveKit bridge)
  POST /api/v1/test/twilio-inbound    Local-only test endpoint that simulates
                                       a Twilio webhook hit. Useful for end-to-
                                       end testing without setting up Twilio.

Idempotency: Twilio retries on non-2xx responses, so we dedupe inbound
messages by MessageSid for 24 hours.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request, Response

from .agents import AGENTS
from .config import settings
from .intent_router import classify
from .models import TestTwilioInboundRequest
from .orchestrator import handle_citizen_message, handle_citizen_voice
from .store import store
from .templates import render as render_template
from .twilio_client import twilio_client
from .twilio_validator import validate_request

log = logging.getLogger("twilio.routes")

router = APIRouter()


# ---------------------------------------------------------------------------
# Idempotency (in-memory, 24h TTL — Redis in production)
# ---------------------------------------------------------------------------

_SEEN: dict[str, float] = {}
_SEEN_LOCK = threading.Lock()
_SEEN_TTL_SEC = 86_400


def _seen_before(msg_sid: str) -> bool:
    if not msg_sid:
        return False
    now = time.time()
    with _SEEN_LOCK:
        # Garbage collect old entries
        if len(_SEEN) > 5000:
            cutoff = now - _SEEN_TTL_SEC
            for k in list(_SEEN.keys()):
                if _SEEN[k] < cutoff:
                    del _SEEN[k]
        if msg_sid in _SEEN:
            return True
        _SEEN[msg_sid] = now
        return False


# ---------------------------------------------------------------------------
# Inbound WhatsApp webhook
# ---------------------------------------------------------------------------

@router.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp_inbound(request: Request) -> Response:
    """Handle an inbound WhatsApp message routed from Twilio.

    Twilio sends application/x-www-form-urlencoded with fields like:
      MessageSid, From, To, Body, NumMedia, MediaUrl0, MediaContentType0,
      ProfileName, WaId, ButtonText, ButtonPayload, Latitude, Longitude
    """
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in form.keys()}

    # 1. Signature validation
    signature = request.headers.get("X-Twilio-Signature")
    # Reconstruct the full URL Twilio used (consider X-Forwarded-Proto/Host
    # if behind a tunnel like ngrok)
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host  = request.headers.get("X-Forwarded-Host",  request.url.netloc)
    full_url = f"{proto}://{host}{request.url.path}"
    if not validate_request(signature_header=signature, url=full_url, params=params):
        log.warning("Rejected Twilio webhook — invalid signature.")
        return Response(status_code=403, content="<Response/>",
                        media_type="application/xml")

    # 2. Idempotency
    msg_sid = params.get("MessageSid", "")
    if _seen_before(msg_sid):
        log.info("Twilio dedup: skipping %s", msg_sid)
        return Response(status_code=200, content="<Response/>",
                        media_type="application/xml")

    # 3. Resolve citizen by msisdn
    from_raw = params.get("From", "")             # e.g. "whatsapp:+91XXXXXXXXXX"
    msisdn = from_raw.replace("whatsapp:", "").lstrip("+")
    if msisdn.startswith("91") and len(msisdn) > 10:
        msisdn = msisdn[-10:]
    if not msisdn.isdigit() or len(msisdn) < 10:
        log.warning("Twilio webhook: bad From=%r", from_raw)
        return Response(status_code=400, content="<Response/>",
                        media_type="application/xml")
    citizen_id = store.get_or_create_citizen(msisdn)

    body = params.get("Body", "")

    # 4. Inspect the message — text? voice note? other media?
    num_media = int(params.get("NumMedia", "0") or "0")
    media_url = params.get("MediaUrl0") if num_media >= 1 else None
    media_ct  = params.get("MediaContentType0", "") if num_media >= 1 else ""

    # 5. Pick agent — by intent classifier
    route = await classify(text=body or "[voice note]", active_agent=None, history=None)
    agent_id = route.primary_agent

    # 6. Dispatch — voice if media is audio, else text
    if media_url and media_ct.startswith("audio/"):
        # Download the audio (Twilio-authed)
        try:
            audio_bytes, content_type = await twilio_client.fetch_media(media_url)
        except Exception as e:
            log.error("Failed to fetch Twilio media: %s", e)
            audio_bytes, content_type = b"", media_ct
        await handle_citizen_voice(
            citizen_id=citizen_id, agent_id=agent_id,
            audio_bytes=audio_bytes, mime_type=content_type or media_ct,
            channel="twilio_wa",
            provider_message_id=msg_sid,
        )
    else:
        await handle_citizen_message(
            citizen_id=citizen_id, agent_id=agent_id,
            text=body, channel="twilio_wa",
            provider_message_id=msg_sid,
        )

    # 7. Respond fast — Twilio expects 200 within 15 seconds; the actual
    # reply will be sent via the Messages API (async).
    return Response(status_code=200, content="<Response/>",
                    media_type="application/xml")


# ---------------------------------------------------------------------------
# Status callback — outbound message lifecycle (queued -> sent -> delivered -> read)
# ---------------------------------------------------------------------------

@router.post("/webhooks/twilio/status")
async def twilio_status_callback(
    MessageSid: str = Form(""),
    MessageStatus: str = Form(""),
    ErrorCode: str = Form(""),
    To: str = Form(""),
) -> Response:
    log.info("Twilio status: sid=%s status=%s to=%s err=%s",
             MessageSid, MessageStatus, To, ErrorCode or "—")
    # Could update Message.providerStatus here based on serverMsgId mapping.
    # Out-of-scope for Phase 3 (we don't keep a sid→msgId index yet).
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Voice webhook — Phase 4 will replace this with a LiveKit SIP bridge
# ---------------------------------------------------------------------------

@router.post("/webhooks/twilio/voice")
async def twilio_voice_inbound(request: Request) -> Response:
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in form.keys()}
    signature = request.headers.get("X-Twilio-Signature")
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host  = request.headers.get("X-Forwarded-Host",  request.url.netloc)
    full_url = f"{proto}://{host}{request.url.path}"
    if not validate_request(signature_header=signature, url=full_url, params=params):
        return Response(status_code=403, content="<Response/>",
                        media_type="application/xml")

    # Phase 3 stub — Phase 4 returns <Connect><Stream> to LiveKit
    # For now we play a brief greeting in English and hang up.
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Aditi" language="en-IN">
    Hello, you have reached the Tamil Nadu government services helpline.
    Live voice calls will be available soon. For now, please send a
    WhatsApp message to this same number. Thank you.
  </Say>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Local test endpoint — simulate a Twilio inbound WITHOUT real Twilio
# ---------------------------------------------------------------------------

@router.post("/api/v1/test/twilio-inbound")
async def test_twilio_inbound(req: TestTwilioInboundRequest) -> dict:
    """POST a fake Twilio webhook to ourselves so the rest of the path is
    exercised without needing a Twilio account. Useful for local testing.
    """
    if not settings.allow_demo_routes:
        raise HTTPException(404, "test endpoint disabled")
    msisdn = req.from_msisdn.lstrip("+").strip()[-10:]
    if not msisdn.isdigit() or len(msisdn) < 10:
        raise HTTPException(400, "bad msisdn")

    citizen_id = store.get_or_create_citizen(msisdn)
    agent_id = req.agent_id
    if not agent_id:
        route = await classify(text=req.body or "[voice]", active_agent=None, history=None)
        agent_id = route.primary_agent
    if agent_id not in AGENTS:
        raise HTTPException(404, f"agent {agent_id} not registered")

    msg = await handle_citizen_message(
        citizen_id=citizen_id, agent_id=agent_id,
        text=req.body, channel="twilio_wa",
        provider_message_id=f"SM_test_{int(time.time())}",
    )
    return {
        "ok": True,
        "citizenId": citizen_id, "msisdn": msisdn,
        "routedTo": agent_id, "serverMsgId": msg.id,
        "convId": msg.convId,
        "note": "Watch the server log — the agent reply will be 'sent' via "
                "Twilio in mock mode (logged only). The WS for this citizen "
                "also receives the streamed reply if connected.",
    }


# ---------------------------------------------------------------------------
# Templates listing (read-only) — used by the Phase 5 admin console
# ---------------------------------------------------------------------------

@router.get("/api/v1/templates")
async def list_templates_route() -> dict:
    from .templates import list_templates
    return {"templates": list_templates()}
