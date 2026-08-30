"""Document upload routes.

POST /api/v1/citizens/{cid}/conversations/{aid}/document
   multipart upload → Sarvam Vision OCR → tool_result-style card →
   agent reply that references the extracted fields.

POST /api/v1/citizens/{cid}/conversations/{aid}/image
   Backward-compatible alias for older clients. Uses the same OCR flow.

POST /api/v1/citizens/{cid}/conversations/{aid}/voice-call-message
   Used by the MOCK call UI: each press-to-talk turn POSTs an audio
   blob and gets back a synthesised voice reply. Same Saaras → LLM →
   Bulbul pipeline as voice notes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .agents import get_agent
from .calls import append_transcript, end_call, get_call
from .config import settings
from .models import Message, SendMessageResponse
from .orchestrator import handle_citizen_image, handle_citizen_voice
from .store import store
from .vision import SUPPORTED_DOC_TYPES

log = logging.getLogger("media.routes")

router = APIRouter()


async def _handle_upload(
    *,
    citizen_id: str,
    agent_id: str,
    upload: UploadFile,
    hint_type: str | None,
    client_msg_id: str | None,
    channel: str,
) -> SendMessageResponse:
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if not getattr(a, "enabled", True):
        raise HTTPException(503, f"{a.name} is temporarily offline for maintenance.")
    if not store.get_citizen(citizen_id):
        raise HTTPException(404, "citizen not found")

    if hint_type and hint_type not in SUPPORTED_DOC_TYPES:
        raise HTTPException(400, f"hint_type must be one of {SUPPORTED_DOC_TYPES}")

    file_bytes = await upload.read()
    if not file_bytes:
        raise HTTPException(400, "document file is empty")
    if len(file_bytes) > 8 * 1024 * 1024:
        raise HTTPException(413, "document too large (max 8 MB)")

    mime = upload.content_type or "application/octet-stream"
    filename = upload.filename or f"upload_{uuid.uuid4().hex[:8]}"

    msg = await handle_citizen_image(
        citizen_id=citizen_id, agent_id=agent_id,
        image_bytes=file_bytes, mime_type=mime,
        filename=filename, hint_type=hint_type,
        client_msg_id=client_msg_id, channel=channel,
    )
    return SendMessageResponse(accepted=True, serverMsgId=msg.id, convId=msg.convId)


@router.post(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/document",
    response_model=SendMessageResponse,
)
async def upload_document(
    citizen_id: str, agent_id: str,
    document: UploadFile = File(...),
    hint_type: str | None = Form(default=None),
    clientMsgId: str | None = Form(default=None),
) -> SendMessageResponse:
    """Upload a document image/PDF and run OCR on it."""
    return await _handle_upload(
        citizen_id=citizen_id, agent_id=agent_id,
        upload=document, hint_type=hint_type,
        client_msg_id=clientMsgId, channel="simulator",
    )


@router.post(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/image",
    response_model=SendMessageResponse,
)
async def upload_image(
    citizen_id: str, agent_id: str,
    image: UploadFile = File(...),
    hint_type: str | None = Form(default=None),
    clientMsgId: str | None = Form(default=None),
) -> SendMessageResponse:
    """Backward-compatible alias for document uploads."""
    return await _handle_upload(
        citizen_id=citizen_id, agent_id=agent_id,
        upload=image, hint_type=hint_type,
        client_msg_id=clientMsgId, channel="simulator",
    )


# Phase 6d — per-citizen serial lock for voice-call turns.
# Background: the VAD-driven simulator can rapid-fire two utterances in
# under a second when the citizen pauses-then-continues. Without
# serialisation, two `_run_agent_turn` tasks run in parallel, each
# generates its own TTS audio, and the citizen hears both replies
# overlapping. This lock ensures only one turn at a time per citizen.
#
# Bonus: if a new utterance arrives while a previous turn is mid-flight,
# we cancel the previous (barge-in semantics — more natural for voice).
import asyncio as _asyncio
_CITIZEN_CALL_TASKS: dict[str, _asyncio.Task] = {}
_CITIZEN_CALL_LOCKS: dict[str, _asyncio.Lock] = {}


def _lock_for(citizen_id: str) -> _asyncio.Lock:
    lock = _CITIZEN_CALL_LOCKS.get(citizen_id)
    if lock is None:
        lock = _asyncio.Lock()
        _CITIZEN_CALL_LOCKS[citizen_id] = lock
    return lock


@router.post(
    "/api/v1/citizens/{citizen_id}/conversations/{agent_id}/call-voice",
    response_model=SendMessageResponse,
)
async def call_voice_turn(
    citizen_id: str, agent_id: str,
    callId: str = Form(...),
    audio: UploadFile = File(...),
    clientMsgId: str | None = Form(default=None),
) -> SendMessageResponse:
    """One voice turn inside a call (VAD-driven utterance from the
    simulator, push-to-talk Twilio bridge, or LiveKit fallback).

    Phase 6d adds a per-citizen serial lock + barge-in cancellation so
    rapid-fire utterances don't produce overlapping agent replies.
    """
    a = get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if not getattr(a, "enabled", True):
        raise HTTPException(503, f"{a.name} is temporarily offline for maintenance.")
    call = get_call(callId)
    if not call:
        raise HTTPException(404, "call not found")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "audio file is empty")

    # Barge-in: if a previous turn for this citizen is still running,
    # cancel it before starting this one. The orchestrator's outer
    # try/except will catch the CancelledError and dispatch nothing —
    # which is exactly what we want (citizen interrupted, drop the old reply).
    prev = _CITIZEN_CALL_TASKS.get(citizen_id)
    if prev and not prev.done():
        prev.cancel()
        try:
            await prev
        except (Exception, _asyncio.CancelledError):
            pass

    async def _do_turn():
        async with _lock_for(citizen_id):
            return await handle_citizen_voice(
                citizen_id=citizen_id, agent_id=agent_id,
                audio_bytes=audio_bytes,
                mime_type=audio.content_type or "audio/webm",
                client_msg_id=clientMsgId, channel="simulator",
            )

    task = _asyncio.create_task(_do_turn())
    _CITIZEN_CALL_TASKS[citizen_id] = task
    try:
        msg = await task
    except _asyncio.CancelledError:
        raise HTTPException(409, "turn cancelled by a newer utterance")
    append_transcript(callId, f"user: {msg.text}")
    return SendMessageResponse(accepted=True, serverMsgId=msg.id, convId=msg.convId)
