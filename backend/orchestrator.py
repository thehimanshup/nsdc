"""Conversation Orchestrator — Phase 3.

Same logic as Phase 2 with one structural change: instead of calling
ws_manager.send_to_citizen() directly for terminal frames, we go through
the channel_dispatcher which delivers frames on the channel the citizen
came in on. WS still gets every frame; Twilio WhatsApp gets only the
final deliverable forms (agent_message, tool_result-rendered-as-text,
broadcast).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("orchestrator")

from .agents import get_agent
from .channel_dispatcher import dispatcher
from .config import settings
from .consent import create_request as create_consent_request
from .coordinator import (
    advance as coord_advance, get_active as coord_get_active,
    match_recipe as coord_match_recipe, open_session as coord_open_session,
)
from .intent_router import classify
from . import latency_metrics as _lat
from .models import Channel, Message
from .rag import retrieve
from .retrieval.pipeline import retrieve_with_meta
from .llm import llm, get_llm_for
from . import personas as _personas
from . import prompt_safety as _ps
from .sarvam_client import detect_language_naive
from .store import store
from .tools import Tool, get_tool, tools_for_agent
from .vision import extract_document
from .voice import stt_transcribe, tts_synthesize
from .ws_manager import ws_manager


# ---------------------------------------------------------------------------
# Per-citizen channel memory
# ---------------------------------------------------------------------------

_TOOL_DETECTION = os.getenv("TOOL_KEYWORD_DETECTION", "true").lower() != "false"
# Hand the tool schemas to the LLM and let it decide (real function-calling).
# When on, the model picks the tool + extracts arguments; the keyword matcher
# above stays as a deterministic backstop. Set TOOL_FUNCTION_CALLING=false to
# fall back to keyword-only selection.
_TOOL_FUNCTION_CALLING = os.getenv("TOOL_FUNCTION_CALLING", "true").lower() != "false"
# OpenAI tool names must match ^[a-zA-Z0-9_-]+$ — our ids use dots, so swap.
_TOOL_NAME_SEP = "__"

_LAST_CHANNEL: dict[str, str] = {}


def _set_last_channel(citizen_id: str, channel: str) -> None:
    _LAST_CHANNEL[citizen_id] = channel


def get_last_channel(citizen_id: str) -> str:
    return _LAST_CHANNEL.get(citizen_id, "simulator")


# ---------------------------------------------------------------------------
# YES/NO consent gating for the WhatsApp channel
# ---------------------------------------------------------------------------

_PENDING_TEXT_CONSENT: dict[str, tuple[str, str, str]] = {}


def pop_pending_text_consent(citizen_id: str) -> Optional[tuple[str, str, str]]:
    return _PENDING_TEXT_CONSENT.pop(citizen_id, None)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def handle_citizen_message(
    *,
    citizen_id: str,
    agent_id: str | None,
    text: str,
    client_msg_id: str | None = None,
    speak_reply: bool = False,
    channel: Channel = "simulator",
    provider_message_id: str | None = None,
) -> Message:
    """Text inbound from any channel. Persists, routes, dispatches reply."""
    citizen = store.get_citizen(citizen_id) or {}
    active_lang = citizen.get("language", "en-IN")
    from .states import detect_state_from_text
    stated = detect_state_from_text(text)
    if stated and stated.code != citizen.get("state_code"):
        store.set_citizen_state(citizen_id, stated.code, stated.primary_language)
        citizen = store.get_citizen(citizen_id) or citizen

    # If we're on Twilio WA and this citizen is awaiting a YES/NO consent
    # decision, intercept the text as the decision instead of a new query.
    if channel == "twilio_wa":
        pending = pop_pending_text_consent(citizen_id)
        if pending:
            req_id, tool_id, original_text = pending
            decision = _parse_yes_no(text)
            if decision is not None:
                from .consent import decide as consent_decide, get_request as get_creq
                creq = get_creq(req_id)
                if creq:
                    consent_decide(req_id, citizen_id, decision)
                    conv_id = store.conv_id(citizen_id, creq.agent_id)
                    asyncio.create_task(resume_after_consent_decision(
                        citizen_id=citizen_id, agent_id=creq.agent_id, conv_id=conv_id,
                        tool_id=tool_id, decision=decision, user_text=original_text,
                        channel="twilio_wa",
                    ))
                    # Synthesize a brief Message reflecting their choice
                    return _persist_user_text(citizen_id, creq.agent_id, text,
                                              active_lang, client_msg_id,
                                              channel, provider_message_id)

    # PHASE 5 — Cross-agent coordinator check.
    # If this is a NEW conversation (no active session) and the text matches
    # a coordinator recipe trigger, OR if the citizen already has an active
    # session, route through the coordinator. Otherwise fall through to
    # single-agent routing as before.
    active_coord = coord_get_active(citizen_id)
    # Phase 6g — URGENT asks never open a multi-step coordinator walkthrough;
    # they get one fast, priority-handled turn with the agent instead.
    matched_recipe = (None if (active_coord or _is_urgent(text))
                      else coord_match_recipe(text))

    if active_coord or matched_recipe:
        if not active_coord:
            active_coord = coord_open_session(citizen_id, matched_recipe.id)
            await _emit_coord_opener(citizen_id, active_coord)
        # Override agent_id to the coordinator's current step
        step = active_coord.current_step
        if step is None:
            # All steps done; close out and fall through
            active_lang = active_lang or "en-IN"
            agent_id = "cmo"
        else:
            agent_id = step.agent_id
            # Phase 6g — sticky resolution. detect_language_naive returned
            # en-IN for ANY Latin text (romanised Hindi included) and
            # flip-flopped the conversation language every turn.
            from .language import resolve_turn_language
            active_lang = resolve_turn_language(text, current_lang=active_lang)
    elif not agent_id:
        route = await classify(text=text, active_agent=None, history=None)
        agent_id = route.primary_agent
        active_lang = route.language
    else:
        from .language import resolve_turn_language
        active_lang = resolve_turn_language(text, current_lang=active_lang)

    if active_lang and active_lang != citizen.get("language"):
        store.set_citizen_language(citizen_id, active_lang)

    _set_last_channel(citizen_id, channel)

    user_msg = _persist_user_text(citizen_id, agent_id, text, active_lang,
                                  client_msg_id, channel, provider_message_id)

    if client_msg_id:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "delivery_ack",
            "clientMsgId": client_msg_id,
            "serverMsgId": user_msg.id, "status": "sent",
        })

    asyncio.create_task(run_agent_turn_dispatch(
        citizen_id=citizen_id, agent_id=agent_id, conv_id=user_msg.convId,
        latest_user_text=text, speak_reply=speak_reply, channel=channel,
    ))
    return user_msg


async def run_agent_turn_dispatch(
    *, citizen_id: str, agent_id: str, conv_id: str, latest_user_text: str,
    speak_reply: bool = False, channel: Channel = "simulator",
    skip_tool_detection: bool = False,
) -> None:
    """Phase 6f — engine selector. Routes single-agent turns to the LangGraph
    engine when ORCHESTRATOR_ENGINE=graph; otherwise (and for coordinator
    sessions or consent-resume) uses the legacy turn loop. Falls back to legacy
    if the graph engine raises, so a graph issue can never break a turn."""
    # Phase 6f-3 — the graph engine now handles coordinator (cross-agent)
    # turns too: handle_citizen_message has already set agent_id to the
    # current step's department; the graph injects the step context and
    # advances the workflow in its post node. Consent-resume stays legacy.
    use_graph = (
        settings.orchestrator_engine == "graph"
        and not skip_tool_detection
    )
    if use_graph:
        try:
            from .graph import runtime as _graph_runtime
            await _graph_runtime.run_turn(
                citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
                latest_user_text=latest_user_text, speak_reply=speak_reply,
                channel=channel)
            return
        except Exception as e:
            log.warning("graph engine error — falling back to legacy: %s", e)
    await _run_agent_turn(
        citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
        latest_user_text=latest_user_text, speak_reply=speak_reply,
        channel=channel, skip_tool_detection=skip_tool_detection)


def _persist_user_text(citizen_id, agent_id, text, lang, client_msg_id,
                       channel, provider_message_id) -> Message:
    conv_id = store.get_or_create_conv(citizen_id, agent_id)
    user_msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="user", type="text",
        text=text, lang=lang,
        timestamp=datetime.utcnow(), clientMsgId=client_msg_id,
        channel=channel, providerMessageId=provider_message_id,
    )
    store.append(user_msg)
    return user_msg


def _parse_yes_no(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if t in ("yes", "y", "ஆம்", "हाँ", "ha", "ok", "okay", "allow"):
        return "granted"
    if t in ("no", "n", "இல்லை", "नहीं", "deny", "denied"):
        return "denied"
    return None


async def handle_citizen_voice(
    *,
    citizen_id: str,
    agent_id: str,
    audio_bytes: bytes,
    mime_type: str,
    client_msg_id: str | None = None,
    channel: Channel = "simulator",
    provider_message_id: str | None = None,
) -> Message:
    """Voice-note inbound. Transcribe via Saaras, then orchestrate.

    Phase 6d voice-quality fix — pass the citizen's state primary
    language as `language_hint` so Saaras biases detection toward it
    (a Tamil Nadu citizen's Hindi-sounding accent gets locked to ta-IN
    OR hi-IN per their state). Without this hint Saaras sometimes
    mis-classifies accented Hindi as English and returns Romanised text.
    """
    citizen = store.get_citizen(citizen_id) or {}
    # Pick the language hint from the citizen's state primary language
    # (it was set on auth/init via /api/v1/auth/init or the state picker)
    citizen_state_code = citizen.get("state_code", "")
    language_hint = ""
    if citizen_state_code:
        from .states import get_state
        s = get_state(citizen_state_code)
        if s:
            language_hint = s.primary_language
    _stt_t0 = time.perf_counter()
    stt = await stt_transcribe(audio_bytes, mime_type=mime_type,
                                 mode="transcribe", language_hint=language_hint)
    _stt_ms = (time.perf_counter() - _stt_t0) * 1000.0
    lang = stt.language or citizen.get("language", "en-IN")
    if lang and lang != citizen.get("language"):
        store.set_citizen_language(citizen_id, lang)

    _set_last_channel(citizen_id, channel)

    conv_id = store.get_or_create_conv(citizen_id, agent_id)
    # Phase 6h — stash STT latency; the turn recorder merges it into this
    # turn's event so the dashboard shows where voice lag comes from.
    _lat.note_stage(conv_id, "stt", _stt_ms)
    audio_url = await _save_audio_blob(audio_bytes, mime_type)

    # If STT itself returned an error message (e.g. "[Sarvam STT HTTP 400: ...]"),
    # short-circuit: persist the voice bubble + a system message explaining the
    # failure, then return without invoking the LLM (which would just compound
    # the error).
    transcript_is_error = stt.transcript.startswith(("[Sarvam STT HTTP", "[STT error"))

    user_msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="user", type="voice",
        text=stt.transcript, lang=lang,
        audioUrl=audio_url, durationSec=stt.duration_s,
        timestamp=datetime.utcnow(), clientMsgId=client_msg_id,
        channel=channel, providerMessageId=provider_message_id,
    )
    store.append(user_msg)

    if transcript_is_error:
        err_msg = Message(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            convId=conv_id, role="system", type="system_event",
            text=("⚠️ Could not transcribe that voice clip. "
                  f"Sarvam STT said: {stt.transcript[:200]}"),
            timestamp=datetime.utcnow(), channel="system",
        )
        store.append(err_msg)
        await dispatcher.dispatch(
            citizen_id=citizen_id,
            frame={"type": "agent_message", "convId": conv_id,
                   "agentId": agent_id, "message": err_msg.model_dump(mode="json")},
            primary_channel=channel,
        )
        return user_msg

    if client_msg_id:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "delivery_ack",
            "clientMsgId": client_msg_id,
            "serverMsgId": user_msg.id, "status": "sent",
        })

    await ws_manager.send_to_citizen(citizen_id, {
        "type": "voice_transcript",
        "convId": conv_id, "messageId": user_msg.id,
        "transcript": stt.transcript, "language": lang,
    })

    # Always synthesize voice reply for voice inbound
    asyncio.create_task(run_agent_turn_dispatch(
        citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
        latest_user_text=stt.transcript, speak_reply=True, channel=channel,
    ))
    return user_msg


async def handle_citizen_image(
    *,
    citizen_id: str,
    agent_id: str,
    image_bytes: bytes,
    mime_type: str,
    filename: str = "",
    hint_type: str | None = None,
    language_hint: str = "",
    client_msg_id: str | None = None,
    channel: Channel = "simulator",
) -> Message:
    """Image / document inbound.

    Flow:
      1. Save the upload to data/uploads (so the chat bubble can display it)
      2. Run Sarvam Vision OCR (real or mock)
      3. Persist a 'media' user message + a 'tool_result'-style card with
         the extracted structured fields
      4. Kick the agent to compose a follow-up reply that references the
         extracted data
    """
    _set_last_channel(citizen_id, channel)
    citizen = store.get_citizen(citizen_id) or {}
    language_hint = language_hint or citizen.get("language", "hi-IN")

    # 1. Save the raw image
    media_url = await _save_media_blob(image_bytes, mime_type, filename)
    media_kind = _media_kind_for(mime_type, filename)

    # 2. Persist the user "media" message
    conv_id = store.get_or_create_conv(citizen_id, agent_id)
    user_msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="user", type="media",
        text=f"[document: {filename or 'upload'}]",
        lang="en-IN",
        mediaUrl=media_url,
        timestamp=datetime.utcnow(),
        clientMsgId=client_msg_id,
        channel=channel,
        extra={"hint_type": hint_type or "auto",
               "size_bytes": len(image_bytes),
               "mime_type": mime_type,
               "filename": filename,
               "media_kind": media_kind},
    )
    store.append(user_msg)

    # ACK to client
    if client_msg_id:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "delivery_ack",
            "clientMsgId": client_msg_id,
            "serverMsgId": user_msg.id, "status": "sent",
        })

    # 3. OCR
    try:
        ocr = await extract_document(
            image_bytes=image_bytes, mime_type=mime_type,
            filename=filename, hint_type=hint_type,
            language_hint=language_hint,
        )
    except Exception as e:
        err_msg = Message(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            convId=conv_id, role="system", type="tool_result",
            text=("[vision] Document verification is temporarily unavailable. "
                  "Please try again later or contact the department counter."),
            timestamp=datetime.utcnow(), lang="en-IN", channel="system",
            extra={"toolId": "vision.extract_document", "ok": False,
                   "error": str(e)[:300]},
        )
        store.append(err_msg)
        await dispatcher.dispatch(
            citizen_id=citizen_id,
            frame={"type": "agent_message", "convId": conv_id,
                   "agentId": agent_id, "message": err_msg.model_dump(mode="json")},
            primary_channel=channel,
        )
        return err_msg

    # 4. Persist the OCR result as a tool_result-style message
    result = {
        "ok": True,
        "document": ocr.fields,
        "document_type": ocr.document_type,
        "confidence": ocr.confidence,
        "language": ocr.language,
        "raw_text": ocr.raw_text,
        "is_mock": ocr.mock,
    }
    ocr_msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="system", type="tool_result",
        text=f"[vision] {ocr.document_type} (confidence {ocr.confidence:.2f})",
        timestamp=datetime.utcnow(),
        lang=ocr.language,
        channel="system",
        extra={"toolId": "vision.extract_document",
               "result": result,
               "redactedSummary": ocr.redacted_for_logs()},
    )
    store.append(ocr_msg)

    await dispatcher.dispatch(
        citizen_id=citizen_id,
        frame={"type": "agent_message", "convId": conv_id,
               "agentId": agent_id, "message": ocr_msg.model_dump(mode="json")},
        primary_channel=channel,
    )

    # 5. Now ask the agent to react — feed it a summary of what was extracted
    extracted_text = _ocr_followup_prompt(ocr)
    asyncio.create_task(_run_agent_turn(
        citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
        latest_user_text=extracted_text, channel=channel,
        skip_tool_detection=True,    # OCR already done — don't trigger Patta-fetch etc.
    ))
    return user_msg


def _media_kind_for(mime: str, original_name: str = "") -> str:
    mime_l = (mime or "").lower()
    name_l = (original_name or "").lower()
    if mime_l.startswith("image/"):
        return "image"
    if mime_l == "application/pdf" or name_l.endswith(".pdf"):
        return "document"
    return "document"


def _ocr_followup_prompt(ocr) -> str:
    """Build the agent-facing summary after OCR.

    When OCR cannot confidently classify the upload, steer the model to ask
    for a clearer re-upload instead of guessing a document type.
    """
    doc_type = (ocr.document_type or "").strip().lower()
    generic = doc_type in {"", "unknown", "_default", "document", "unclassified"}
    fields_text = json.dumps(ocr.fields or {}, ensure_ascii=False)
    parts: list[str] = []

    if generic:
        parts.append(
            "The citizen uploaded a document, but OCR could not confidently "
            "identify the document type."
        )
    else:
        parts.append(f"The citizen uploaded a {ocr.document_type}.")

    parts.append(f"Extracted fields: {fields_text}.")
    parts.append(f"Confidence: {ocr.confidence:.2f}.")

    if ocr.raw_text:
        parts.append(f"OCR text: {ocr.raw_text[:600]}")

    if generic:
        parts.append(
            "Do NOT guess that it is Aadhaar, PAN, driving licence, or any "
            "other document. Tell the citizen the image was not clear enough "
            "to read, and ask them to re-upload a clearer photo or select the "
            "document type from the attach menu."
        )
    else:
        parts.append(
            "Please confirm receipt, briefly summarise what was recognised, "
            "and ask what they want to do next."
        )

    return " ".join(parts)


async def _save_media_blob(blob: bytes, mime: str, original_name: str = "") -> str:
    if not blob:
        return ""
    ext = Path(original_name or "").suffix.lower()
    if not ext:
        mime_l = (mime or "").lower()
        if mime_l.startswith("image/jpeg") or mime_l.startswith("image/jpg"):
            ext = ".jpg"
        elif "png" in mime_l:
            ext = ".png"
        elif "webp" in mime_l:
            ext = ".webp"
        elif "pdf" in mime_l:
            ext = ".pdf"
        elif "heic" in mime_l:
            ext = ".heic"
        elif "tiff" in mime_l:
            ext = ".tiff"
        elif "gif" in mime_l:
            ext = ".gif"
        else:
            ext = ".bin"
    name = f"img_{uuid.uuid4().hex[:14]}{ext}"
    d = Path(settings.data_dir) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob)
    return f"/api/v1/uploads/{name}"


# ---------------------------------------------------------------------------
# Internal — single turn
# ---------------------------------------------------------------------------

async def _run_agent_turn(
    *,
    citizen_id: str,
    agent_id: str,
    conv_id: str,
    latest_user_text: str,
    speak_reply: bool = False,
    channel: Channel = "simulator",
    skip_tool_detection: bool = False,
) -> None:
    """Run a single agent turn.

    Wrapped in a top-level try/except so ANY uncaught error still clears
    the citizen's typing indicator and delivers a fallback message —
    otherwise the chat hangs forever showing "..." (the symptom we hit
    on the Phase 6d roll-up when `log` wasn't defined).
    """
    try:
        await _run_agent_turn_impl(
            citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
            latest_user_text=latest_user_text, speak_reply=speak_reply,
            channel=channel, skip_tool_detection=skip_tool_detection,
        )
    except Exception as e:
        log.exception("Uncaught error in _run_agent_turn for citizen=%s agent=%s: %s",
                       citizen_id, agent_id, e)
        # Clear typing + send a safe fallback so the chat doesn't hang
        try:
            await ws_manager.send_to_citizen(citizen_id, {
                "type": "agent_typing", "convId": conv_id,
                "agentId": agent_id, "isTyping": False,
            })
            fallback_msg = Message(
                id=f"msg_{uuid.uuid4().hex[:12]}",
                convId=conv_id, role="agent", type="text",
                text=("Sorry, I hit a snag on my side. Please try again in a moment."),
                lang="en-IN", timestamp=datetime.utcnow(), channel="system",
            )
            store.append(fallback_msg)
            await dispatcher.dispatch(
                citizen_id=citizen_id,
                frame={"type": "agent_message", "convId": conv_id,
                       "agentId": agent_id,
                       "message": fallback_msg.model_dump(mode="json")},
                primary_channel=channel,
            )
        except Exception:
            log.exception("Failed even to dispatch fallback for citizen=%s", citizen_id)


async def _run_agent_turn_impl(
    *,
    citizen_id: str,
    agent_id: str,
    conv_id: str,
    latest_user_text: str,
    speak_reply: bool = False,
    channel: Channel = "simulator",
    skip_tool_detection: bool = False,
) -> None:
    _turn_t0 = time.perf_counter()
    _stages: dict[str, float] = {}

    agent = get_agent(agent_id)
    if not agent:
        return

    from .states import detect_state_from_text
    stated = detect_state_from_text(latest_user_text or "")
    if stated:
        citizen_now = store.get_citizen(citizen_id) or {}
        if stated.code != citizen_now.get("state_code"):
            store.set_citizen_state(citizen_id, stated.code, stated.primary_language)

    await ws_manager.send_to_citizen(citizen_id, {
        "type": "agent_typing", "convId": conv_id,
        "agentId": agent_id, "isTyping": True,
    })

    corpus_id = agent.corpus_id or agent.id
    # Phase 6c/6d — structured retrieval with cross-corpus reads + scores +
    # state filtering. Chunks tagged with the citizen's state_code or
    # "central" pass through; other states' chunks are excluded.
    cross = list(getattr(agent, "cross_corpus_read", []) or [])
    _citizen_state_pre = (store.get_citizen(citizen_id) or {}).get("state_code", "")
    with _lat.stage(_stages, "rag"):
        raw_hits = retrieve_with_meta(corpus_id, latest_user_text, k=4,
                                      extra_corpora=cross or None,
                                      state_code=_citizen_state_pre)
    # Hallucination guard — if the top-1 score is low, we instruct the
    # agent to be honest about not having info rather than guessing.
    top_score = raw_hits[0][1] if raw_hits else 0.0
    low_confidence = top_score < 0.5
    chunks = [c for c, _ in raw_hits]
    rag_context = "\n\n".join(c.to_context_block() for c in chunks) if chunks else ""
    # Build citation payload for the simulator (rendered as chips)
    citations_payload = [c.as_citation() for c in chunks]

    citizen = store.get_citizen(citizen_id) or {}
    citizen_lang = citizen.get("language", "en-IN")
    citizen_state_code = citizen.get("state_code", "")

    # Phase 6d — auto language detection from the latest user text.
    # Phase 6g — switched to STICKY resolution: only change the conversation
    # language on a confident signal (native script, romanised-Indic markers,
    # or clearly-English text); ambiguous turns ("ok", "123") keep the
    # current language instead of flip-flopping.
    from .language import (resolve_turn_language,
                           system_prompt_language_instruction)
    from .states import get_state
    state_obj = get_state(citizen_state_code) if citizen_state_code else None
    state_default_lang = state_obj.primary_language if state_obj else "en-IN"
    detected_lang = resolve_turn_language(
        latest_user_text or "", current_lang=citizen_lang,
        state_default=state_default_lang,
    )
    if detected_lang != citizen_lang:
        store.set_citizen_language(citizen_id, detected_lang)
        citizen_lang = detected_lang

    # Phase 6d — diagnostic. One log line per turn shows the resolved
    # state + language + retrieval state so an operator can debug why an
    # agent picked the language/persona/citations it did.
    log.info(
        "turn citizen=%s agent=%s state=%s lang=%s rag_top_score=%.2f "
        "rag_hits=%d cross_corpus=%s",
        citizen_id, agent_id, citizen_state_code or "(unset)",
        citizen_lang, top_score, len(chunks), ",".join(cross) or "-",
    )

    # Phase 6c — conversation-continuity block. Tells the model whether
    # this is a first turn (greet warmly) or a continuation (DON'T greet
    # again — just answer). Without this, the model treats every short
    # utterance as a new conversation and repeats the persona's opener.
    prior_msgs = store.conversations.get(conv_id, [])
    prior_agent_replies = sum(1 for m in prior_msgs if m.role == "agent")

    # Phase 7 — topical-scope guardrail. Refuse clearly off-topic asks (jokes,
    # puzzles, code, roleplay, …) BEFORE spending the agent LLM on them. Skipped
    # for consent-resume / OCR follow-ups (skip_tool_detection).
    if settings.scope_guard_enabled and not skip_tool_detection:
        from . import scope_guard
        _hist = store.as_chat_messages(conv_id, limit=6) if prior_agent_replies else None
        _verdict = await scope_guard.check(latest_user_text, agent_id=agent_id,
                                           history=_hist)
        if not _verdict.in_scope:
            from . import audit as _audit
            _audit.append_event(
                actor=citizen_id, action="security.off_topic_blocked",
                resource={"citizenId": citizen_id, "agentId": agent_id,
                          "engine": "legacy"},
                payload={"category": _verdict.category,
                         "confidence": _verdict.confidence, "via": _verdict.via,
                         "preview": (latest_user_text or "")[:160]})
            _refusal = await scope_guard.refusal(
                agent_id=agent_id, lang=citizen_lang, category=_verdict.category)
            await _emit_refusal(
                citizen_id, agent_id, conv_id, text=_refusal, lang=citizen_lang,
                channel=channel, speak_reply=speak_reply, state_obj=state_obj,
                persona=agent.resolve_persona(citizen_state_code, voice_seed=conv_id))
            return

    from .conversation_quality import (
        detect_slot_loop, extract_known_facts, postprocess_citizen_reply,
        render_behavior_contract,
    )
    known_facts = extract_known_facts(prior_msgs, latest_user_text)
    slot_loop = detect_slot_loop(prior_msgs, latest_user_text)
    behavior_block = render_behavior_contract(
        channel=channel, speak_reply=speak_reply, detected_lang=citizen_lang,
        known_facts=known_facts, slot_loop=slot_loop,
    )
    if prior_agent_replies == 0:
        continuity_block = (
            "Conversation state: this is the FIRST exchange in this conversation. "
            "Greet warmly in your own words (don't read out the example opener "
            "verbatim — say it your own way), then ask how you can help."
        )
    else:
        continuity_block = (
            f"Conversation state: you have ALREADY been talking with this citizen "
            f"({prior_agent_replies} agent replies so far). DO NOT greet again. "
            f"DO NOT introduce yourself again. Just continue the conversation — "
            f"answer the citizen's latest question directly. If the latest message "
            f"is unclear or just a sound like 'hmm', ask a brief clarifying question "
            f"in your own voice (don't repeat your opener)."
        )

    # PHASE 5 — Coordinator context injection.
    # If this citizen has an active coordinator session, inject the
    # current step's purpose + accumulated shared_context into the
    # agent's system prompt so it knows its role in the bigger flow.
    coord_block = ""
    coord_session = coord_get_active(citizen_id)
    if coord_session and coord_session.recipe_id and not coord_session.completed:
        step = coord_session.current_step
        if step and step.agent_id == agent_id:
            coord_block = (
                f"\n\nCROSS-AGENT COORDINATOR — you are step "
                f"{coord_session.current_step_idx + 1}/"
                f"{len(coord_session.recipe.steps)} of "
                f"'{coord_session.recipe.title}'.\n"
                f"Your role: {step.purpose}\n"
                f"Step-specific instructions: {step.inject_context}\n"
                f"Shared context from earlier steps: "
                f"{json.dumps(coord_session.shared_context, ensure_ascii=False)[:600]}"
            )

    # Phase 6c — pick few-shot examples by similarity to the citizen's query.
    # The persona block in the system prompt teaches the LLM the voice; the
    # few-shot pairs anchor it concretely.
    # Phase 6f — on a spoken turn, prefer the voice example bank so the agent
    # mimics a natural human-on-the-phone manner (short, no lists/URLs).
    few_shot_block = _personas.render_few_shot_block(
        agent_id, latest_user_text, n=3, voice=speak_reply, lang=citizen_lang)

    # Phase 6b — fence the most recent user text + sanitize the RAG block
    # so neither can break out of the data layer.
    safe_rag_context = _ps.neutralise_fence_sentinels(rag_context)

    # Phase 6c — channel-specific tone block (voice gets shorter sentences,
    # no citation markers spoken, etc.).
    tone_block = _personas.channel_tone_block(channel)

    # Phase 6c — hallucination guard line.
    guard_line = ""
    if low_confidence:
        guard_line = (
            "\n\nIMPORTANT — RAG returned LOW-CONFIDENCE hits for this query. "
            "Do NOT invent scheme amounts, eligibility, or dates. If the "
            "OFFICIAL CONTEXT above doesn't really answer the citizen's "
            "question, say so honestly: 'I don't have current data on that — "
            "let me connect you to a human officer or you can call the "
            "department helpline.'"
        )

    # Phase 6d — state-aware system prompt + language instruction
    state_label = (f"{state_obj.name} ({state_obj.code})" if state_obj
                   else "India (state unknown)")
    # Phase 6d — pass channel so voice calls get a stronger
    # "reply in native script" instruction (otherwise Bulbul TTS
    # mispronounces Romanised text).
    voice_channel = "voice" if speak_reply else channel
    lang_instruction = system_prompt_language_instruction(
        citizen_lang, citizen_state_code, channel=voice_channel,
    )
    base_system = agent.system_prompt(
        rag_context=safe_rag_context,
        few_shot_block=few_shot_block,
        conversation_continuity_block=continuity_block,
        state_code=citizen_state_code,
        voice_seed=conv_id,
    ) + (
        f"\n\nCITIZEN CONTEXT:\n"
        f"- state: {state_label}\n"
        f"- detected language: {citizen_lang}\n"
        f"- channel: {channel}\n"
        f"- {lang_instruction}\n"
        f"\n{tone_block}\n\n{behavior_block}"
    ) + coord_block + guard_line
    # Phase 6g — URGENT short-circuit. Validation: "when asking for urgent
    # help there are 3 steps" — the multi-step recipe walkthrough is wrong for
    # an emergency. One turn: safety first, helpline, then WE do the filing.
    if _is_urgent(latest_user_text):
        base_system += (
            "\n\nURGENT REQUEST — OVERRIDE NORMAL FLOW:\n"
            "The citizen needs urgent help. Do NOT walk them through a "
            "multi-step process or list departments. In ONE short reply: "
            "(1) if there is any risk to life/safety, give the right emergency "
            "number FIRST (112 emergency, 108 ambulance, 1077 district disaster "
            "control, 1098 children, 181 women); (2) tell them the case is being "
            "registered on priority RIGHT NOW on their behalf (the system files "
            "it — never invent the reference number yourself); (3) at most ONE "
            "question, only if truly needed. Reassure, don't lecture."
        )
    # Phase 7 — attached skills (legacy-engine parity with the graph engine).
    # Inject each wired skill's instruction fragment so the agent knows how to
    # use the tools the skill brought. Capped to keep the prompt bounded; the
    # skill's tools are made selectable separately via _agent_tools().
    if settings.skills_enabled:
        try:
            from .skills import skills_for_agent
            _skills = skills_for_agent(agent_id)
            _shown = _skills[:4]
            if len(_skills) > 4:
                log.warning("agent %s has %d skills attached; injecting first 4",
                            agent_id, len(_skills))
            _blocks = [f"- {s.name}: {s.instructions}".rstrip(": ").rstrip()
                       for s in _shown if s.instructions]
            if _blocks:
                base_system += ("\n\nSKILLS — extra capabilities available to you "
                                "this turn:\n" + "\n".join(_blocks))
        except Exception:
            pass
    system_prompt = _ps.augment_system_prompt(base_system, agent.name)

    # In mock mode we keyword-match a tool. We DON'T do this if we just
    # resumed after a consent decision — otherwise the same tool would
    # be re-triggered on the original user text in an infinite loop.
    # Phase 6e — tool detection now runs in BOTH mock and live mode. Sarvam
    # native function-calling isn't wired, so the keyword matcher is the only
    # path that lets an agent actually DO something (file/track a record,
    # search schemes, fetch a document) during a real conversation. Set
    # TOOL_KEYWORD_DETECTION=false to disable.
    # Phase 6b — pick the per-agent LLM if the agent has one pinned. Resolved
    # here (earlier than the reply stream) because function-calling tool
    # selection below needs it too.
    agent_llm = get_llm_for(getattr(agent, "llm_provider", None))

    tool_ctx: Optional[tuple] = None   # (tool, result) — grounds the reply
    if not skip_tool_detection and (_TOOL_FUNCTION_CALLING or _TOOL_DETECTION):
        # Hybrid: the model picks the tool + extracts args via function-calling;
        # the keyword matcher is the deterministic backstop. See _select_tool.
        tool, tool_args = await _select_tool(
            agent_id, agent_llm, conv_id, latest_user_text, channel,
            voice=speak_reply)
        if tool and tool.requires_consent:
            await _send_consent_request(citizen_id, agent_id, conv_id, tool,
                                        latest_user_text, channel)
            return
        elif tool:
            with _lat.stage(_stages, "tool"):
                _tool_res = await _execute_tool_and_append(
                    citizen_id, conv_id, tool, args=tool_args,
                    channel=channel, return_result=True,
                )
            tool_ctx = (tool, _tool_res or {})
            # Phase 6f — feed the REAL tool output (record_id, status, scheme
            # names) into the prompt so the agent quotes facts, never invents a
            # ticket number. The chat history path drops tool messages, so this
            # grounding is the only way the model learns what actually happened.
            system_prompt += _tool_grounding_block(tool_ctx, citizen_lang)

    # Phase 6b — fence the LATEST user message. Earlier history stays as-is
    # because it's already been audited / sanitized through this same path.
    fenced = _ps.fence_user_input(latest_user_text)
    if fenced.suspicious:
        # Log a security event so DPO/auditor can see attempted jailbreaks
        from . import audit as _audit
        _audit.append_event(
            actor=citizen_id, action="security.prompt_injection_attempt",
            resource={"citizenId": citizen_id, "agentId": agent_id, "convId": conv_id},
            payload={"hits": fenced.injection_hits,
                     "preview": (latest_user_text or "")[:160]},
        )

    msgs = [{"role": "system", "content": system_prompt}]
    history = store.as_chat_messages(conv_id, limit=8)
    # Replace the most recent user turn with the fenced version so the
    # LLM sees the safety wrapper. Older turns stay plain for natural flow.
    if history and history[-1].get("role") == "user":
        history[-1] = {"role": "user", "content": fenced.fenced_text}
    else:
        history.append({"role": "user", "content": fenced.fenced_text})
    msgs.extend(history)

    server_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    full_chunks: list[str] = []
    first_token = False

    async def _stream_once(into_chunks: list[str]) -> Optional[Exception]:
        """Run the LLM stream once, appending deltas. Returns exception if any."""
        nonlocal first_token
        try:
            async for delta in agent_llm.chat_stream(messages=msgs):
                into_chunks.append(delta)
                if not first_token:
                    # Phase 6h — time-to-first-token (perceived responsiveness)
                    _stages.setdefault(
                        "llm_first", (time.perf_counter() - _llm_t0) * 1000.0)
                    await ws_manager.send_to_citizen(citizen_id, {
                        "type": "agent_typing", "convId": conv_id,
                        "agentId": agent_id, "isTyping": False,
                    })
                    first_token = True
                # Stream deltas to the chat UI. The simulator replaces
                # the rendered text with the post-stream cleaned text on
                # `agent_message`, so any reasoning that streams through
                # gets corrected at the end.
                await ws_manager.send_to_citizen(citizen_id, {
                    "type": "agent_token", "convId": conv_id,
                    "agentId": agent_id, "serverMsgId": server_msg_id,
                    "delta": delta,
                })
        except Exception as exc:
            return exc
        return None

    _llm_t0 = time.perf_counter()
    exc = await _stream_once(full_chunks)
    stream_exception = exc

    # Phase 6c — if the stream produced NO content at all (Sarvam sometimes
    # does this on concurrent calls), retry ONCE before giving up. This
    # eliminates the "I'm having trouble" message when two simulators talk
    # at the same time.
    if not full_chunks and exc is None:
        log.warning("Sarvam stream empty for citizen=%s agent=%s — retrying once",
                    citizen_id, agent_id)
        import asyncio as _a
        # Phase 6e — on a live voice call every millisecond of dead air is
        # audible, so the back-off before the single retry is much shorter
        # for spoken turns than for chat.
        await _a.sleep(0.12 if speak_reply else 0.4)
        exc = await _stream_once(full_chunks)
        stream_exception = exc

    if exc is not None:
        log.error("Sarvam stream failed for citizen=%s agent=%s: %s",
                  citizen_id, agent_id, exc)
        full_chunks = [f"(connection issue: {exc})"]

    # Phase 6h — full LLM stream duration (includes the single retry, since
    # that's the latency the citizen actually experienced).
    _stages["llm_total"] = (time.perf_counter() - _llm_t0) * 1000.0
    _post_t0 = time.perf_counter()

    # Phase 6d — state-aware persona for fallback messages
    resolved_persona = agent.resolve_persona(citizen_state_code, voice_seed=conv_id)
    # Mark every non-real-content path as a fallback so citations get
    # suppressed (a "(connection issue)" or "Sorry, didn't catch that"
    # message with 4 scheme citations is more confusing than helpful).
    used_fallback = (stream_exception is not None)

    full_text = "".join(full_chunks).strip()
    if not full_text:
        # Truly empty — context-aware fallback using state-aware persona
        used_fallback = True
        persona_name = resolved_persona["persona_name"].split(",")[0]
        if prior_agent_replies == 0:
            full_text = (resolved_persona.get("signature_opener") or
                         f"Vanakkam! {persona_name} here. How can I help you today?")
        else:
            full_text = ("Sorry, my line was a bit choppy there — could you "
                          "say that again?")
        log.warning("Empty stream — used state-aware persona fallback for %s/%s",
                    agent_id, citizen_state_code or "default")

    # Phase 6c — context-aware safe fallback. On the FIRST turn use the
    # state-aware persona opener; on continuation turns use a polite
    # "didn't catch that".
    if prior_agent_replies == 0:
        persona_fallback = (
            resolved_persona.get("signature_opener") or
            f"Vanakkam! Welcome to {agent.name}. How can I help you today?"
        )
    else:
        persona_fallback = (
            "Sorry, I didn't quite catch that — could you say it again "
            "or describe what you need help with?"
        )

    # Phase 6b — scan the response for leaks / persona breaks before
    # persisting & dispatching. On a hit we replace with the persona's
    # opener and log a security event.
    leak = _ps.scan_output_for_leakage(full_text, fallback=persona_fallback)
    if not leak.ok:
        from . import audit as _audit
        _audit.append_event(
            actor=agent_id, action="security.output_leakage_blocked",
            resource={"citizenId": citizen_id, "agentId": agent_id, "convId": conv_id},
            payload={"matches": leak.matches,
                     "blocked_preview": full_text[:200]},
        )
        full_text = leak.safe_text

    # Phase 6c — detect chain-of-thought reasoning leakage (Sarvam sometimes
    # spits out its analysis steps instead of just the reply).
    reasoning = _ps.detect_and_strip_reasoning(full_text, fallback=persona_fallback)
    if reasoning.leaked:
        from . import audit as _audit
        _audit.append_event(
            actor=agent_id, action="quality.reasoning_leak_stripped",
            resource={"citizenId": citizen_id, "agentId": agent_id, "convId": conv_id},
            payload={"matches": reasoning.matches,
                     "fallback_used": reasoning.fallback_used,
                     "raw_preview": full_text[:300]},
        )
        full_text = reasoning.cleaned_text

    # Phase 6d — Romanised Indic detector. If Sarvam slipped into
    # Romanised Hindi/Tamil/Bengali/etc. despite our instructions, we
    # transliterate it to the native script via Sarvam-Translate. This:
    #   (a) makes the chat text readable in the citizen's native script
    #   (b) lets Bulbul TTS pronounce naturally in voice replies
    # On failure we fall back silently — the reply is never dropped.
    if not used_fallback:
        from .language import (detect_romanised_indic, enforce_reply_language,
                               transliterate_to_native)
        rom_lang = detect_romanised_indic(full_text)
        if rom_lang:
            log.warning("Romanised %s detected in reply — transliterating: %r",
                        rom_lang, full_text[:120])
            converted = await transliterate_to_native(full_text, rom_lang)
            if converted and converted != full_text:
                from . import audit as _audit
                _audit.append_event(
                    actor=agent_id, action="quality.transliterated_to_native",
                    resource={"citizenId": citizen_id, "agentId": agent_id,
                              "convId": conv_id, "targetLang": rom_lang},
                    payload={"original_preview": full_text[:200],
                              "converted_preview": converted[:200]},
                )
                full_text = converted
                citizen_lang = rom_lang   # so TTS picks the right Bulbul lang
        corrected = await enforce_reply_language(full_text, citizen_lang)
        if corrected and corrected != full_text:
            log.warning("Reply language drift corrected to %s: %r",
                        citizen_lang, full_text[:120])
            full_text = corrected

    # Phase 6f — anti-fabrication guard. The agent must never invent a
    # reference/ticket number (the transcript showed a made-up "G-2024-00567").
    # If a record-creating tool ran this turn, force the REAL id into the reply;
    # otherwise strip any fabricated reference token so we never hand the
    # citizen a number that doesn't exist in admin/ops.
    if not used_fallback and full_text:
        real_id = ""
        if tool_ctx and isinstance(tool_ctx[1], dict):
            real_id = (tool_ctx[1].get("record_id") or "")
        full_text = _fix_fabricated_refs(full_text, citizen_id, real_id,
                                         lang=citizen_lang)

    # Phase 6e — anti-repetition guard. Sarvam-30B sometimes echoes its own
    # previous turn almost verbatim; on a voice call that comes across as the
    # agent "repeating statements". If this reply is near-identical to the
    # agent's last reply in this conversation, swap in a short varied
    # follow-up instead of saying the same thing twice.
    if not used_fallback and full_text:
        prev_agent = next((m.text for m in reversed(prior_msgs)
                           if m.role == "agent" and (m.text or "").strip()), "")
        if prev_agent and _is_near_duplicate(full_text, prev_agent):
            log.info("Near-duplicate reply suppressed for citizen=%s agent=%s",
                     citizen_id, agent_id)
            # Only substitute when we have a safe phrasing in the reply
            # language (English). For other languages, repeating is still
            # better than an English line mid-call, so we leave it.
            if (citizen_lang or "en-IN").startswith("en"):
                full_text = "Is there anything else I can help you with?"

    agent_msg = Message(
        id=server_msg_id, convId=conv_id, role="agent", type="text",
        text=full_text, lang=citizen_lang, timestamp=datetime.utcnow(),
        channel="system",
    )
    # Phase 6c/6d — attach citations + retrieval metadata so the simulator
    # can render source chips below the agent bubble. Do NOT attach
    # citations when the LLM produced no real content and we used a
    # persona fallback — they're confusing because the fallback message
    # has nothing to do with the retrieved chunks.
    if citations_payload and not used_fallback:
        agent_msg.extra = {
            **(agent_msg.extra or {}),
            "citations": citations_payload,
            "ragTopScore": round(top_score, 4),
            "ragLowConfidence": low_confidence,
            "ragCrossCorpus": cross,
        }
    # Phase 6d — attach state + detected language so the simulator can
    # show a language pill and the citizen's state badge per turn.
    agent_msg.extra = {
        **(agent_msg.extra or {}),
        "detectedLanguage": citizen_lang,
        "stateCode": citizen_state_code or None,
        "stateName": state_obj.name if state_obj else None,
        "personaName": resolved_persona["persona_name"],
    }

    # Phase 6h — post-processing time (safety scans, transliteration,
    # anti-repeat / anti-fabrication guards) between LLM end and TTS start.
    _stages["post"] = (time.perf_counter() - _post_t0) * 1000.0

    if speak_reply:
        # On voice replies, strip citation markers like "[1]" or "[SOURCE: …]"
        # since TTS would read them out loud awkwardly.
        spoken = re.sub(r"\[SOURCE:[^\]]*\]", "", full_text)
        spoken = re.sub(r"\[\d+\]", "", spoken).strip()
        # Phase 6d — use the state-aware persona's voice and pick Bulbul
        # language by the SCRIPT of the actual reply text (so a Devanagari
        # reply always uses hi-IN voice synthesis even if the citizen's
        # earlier detection was different).
        from .language import tts_language_for
        persona_voice = resolved_persona.get("voice") or agent.voice
        tts_lang = tts_language_for(citizen_lang, state_obj, reply_text=spoken)
        with _lat.stage(_stages, "tts"):
            tts = await tts_synthesize(
                spoken or full_text,
                target_language_code=tts_lang,
                speaker=persona_voice,
            )
        if tts.audio_bytes:
            audio_url = await _save_audio_blob(tts.audio_bytes, tts.mime)
            agent_msg.audioUrl = audio_url
            agent_msg.durationSec = tts.duration_s
        agent_msg.extra = {
            **(agent_msg.extra or {}),
            "ttsMock": tts.mock,
            "ttsError": tts.error or None,
            "ttsVoice": persona_voice,
            "ttsLanguage": tts_lang,
        }

    store.append(agent_msg)

    # Dispatch the final agent message — fans to WS + Twilio as appropriate
    await dispatcher.dispatch(
        citizen_id=citizen_id,
        frame={
            "type": "agent_message", "convId": conv_id,
            "agentId": agent_id, "message": agent_msg.model_dump(mode="json"),
        },
        primary_channel=channel,
    )

    await ws_manager.send_to_citizen(citizen_id, {
        "type": "agent_typing", "convId": conv_id,
        "agentId": agent_id, "isTyping": False,
    })

    # Phase 6h — record this turn's latency breakdown for the admin dashboard.
    try:
        _stages["total"] = (time.perf_counter() - _turn_t0) * 1000.0
        _lat.record_turn(
            conv_id=conv_id, agent_id=agent_id, channel=channel,
            stages=_stages, speak_reply=speak_reply, lang=citizen_lang,
            tool_id=(tool_ctx[0].id if tool_ctx else ""),
            fallback=used_fallback, citizen_id=citizen_id,
        )
    except Exception:
        log.exception("latency record failed (non-fatal)")

    # PHASE 5 — Coordinator step advance.
    # If this citizen has an active coordinator session and the step we
    # just ran was its current step, mark the step complete and emit a
    # `coordinator_state` frame so the simulator updates the progress UI.
    coord_session = coord_get_active(citizen_id)
    if coord_session and coord_session.current_step \
            and coord_session.current_step.agent_id == agent_id:
        contribution = {f"step_{coord_session.current_step_idx}_agent": agent_id,
                        f"step_{coord_session.current_step_idx}_reply_id": agent_msg.id}
        new_state = coord_advance(coord_session.session_id, contribution=contribution)
        if new_state:
            await ws_manager.send_to_citizen(citizen_id, {
                "type": "coordinator_state",
                "progress": new_state.progress(),
            })
            if new_state.completed:
                done_msg = Message(
                    id=f"msg_{uuid.uuid4().hex[:12]}",
                    convId=conv_id, role="system", type="system_event",
                    text=(f"✅ {new_state.recipe.title} complete — "
                          f"{len(new_state.recipe.steps)} agents collaborated. "
                          f"You'll receive updates as this progresses."),
                    timestamp=datetime.utcnow(), channel="system",
                )
                store.append(done_msg)
                await dispatcher.dispatch(
                    citizen_id=citizen_id,
                    frame={"type": "agent_message", "convId": conv_id,
                           "agentId": agent_id,
                           "message": done_msg.model_dump(mode="json")},
                    primary_channel=channel,
                )


async def _emit_coord_opener(citizen_id: str, sess) -> None:
    """Emit a system-style opener when the coordinator first kicks in."""
    conv_id = store.conv_id(citizen_id, sess.current_step.agent_id) \
        if sess.current_step else None
    if not conv_id:
        return
    msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="system", type="system_event",
        text=(f"🔄 Starting cross-agent flow: {sess.recipe.title}. "
              f"{len(sess.recipe.steps)} departments will collaborate "
              f"({', '.join(s.agent_id for s in sess.recipe.steps)})."),
        timestamp=datetime.utcnow(), channel="system",
    )
    store.append(msg)
    await ws_manager.send_to_citizen(citizen_id, {
        "type": "coordinator_state",
        "progress": sess.progress(),
    })
    await ws_manager.send_to_citizen(citizen_id, {
        "type": "agent_message", "convId": conv_id,
        "agentId": sess.current_step.agent_id,
        "message": msg.model_dump(mode="json"),
    })


BULBUL_LANGS = {"hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN",
                "bn-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN", "en-IN"}


# ---------------------------------------------------------------------------
# Tool & consent helpers
# ---------------------------------------------------------------------------

# Phase 6g — urgency detection. Urgent asks skip the multi-step coordinator
# walkthrough and get a single priority-handled turn instead.
_URGENT_RE = re.compile(
    r"\b(urgent|emergency|immediately|right now|asap|life.?(or|and).?death"
    r"|dying|critical|accident|collapsed?|trapped|flood(ing)? (now|in my)"
    r"|fire\b|drowning|unconscious|bleeding|severe)\b"
    r"|तुरंत|आपातकाल|इमरजेंसी|जल्दी करो|बहुत ज़रूरी|अर्जेंट"
    r"|அவசரம்|உடனடியாக|எமர்ஜென்சி", re.I | re.UNICODE)


def _is_urgent(text: str) -> bool:
    return bool(_URGENT_RE.search(text or ""))


# Phase 6e — reference-number pattern (GRV-TN-2026-000123, APP-…, SRV-…, PRJ-…)
_RECORD_ID_RE = re.compile(r"\b((?:GRV|APP|PRJQ|SRV|REC)-[A-Z]{2}-\d{4}-\d{4,6})\b", re.I)
_PROJECT_ID_RE = re.compile(r"\b(PRJ-[A-Z]{2}-[A-Z]{2,4}-\d{4}-\d{3,5})\b", re.I)


def _is_advice_question(text: str) -> bool:
    t = (text or "").lower()
    return bool(re.search(
        r"\b(what (should|to) (i|we) do|what to do|what can i do|how should i proceed|"
        r"please advise|guide me|what are my options)\b",
        t,
    ))


def _has_explicit_filing_request(text: str) -> bool:
    t = (text or "").lower()
    return bool(re.search(
        r"\b(file|register|raise|lodge|submit|create|open)\b.{0,24}"
        r"\b(complaint|grievance|case|fir|ticket|issue)\b"
        r"|\b(escalate|complain|grievance|ticket)\b"
        r"|darj kar|दर्ज कर|शिकायत दर्ज|புகார்|பதிவு செய",
        t,
        flags=re.UNICODE,
    ))


def _agent_tools(agent_id: str) -> list[Tool]:
    """Tools available to an agent THIS turn = its operator-wired tools PLUS any
    tools brought by attached skills. Mirrors the graph engine's lc_tools so a
    skill behaves identically on the legacy engine: a wired skill's tool_ids
    become selectable (function-calling schemas + keyword matcher) for that
    agent. Deduped by id; a skill referencing an unregistered tool id is
    skipped."""
    tools = list(tools_for_agent(agent_id))
    if not settings.skills_enabled:
        return tools
    seen = {t.id for t in tools}
    try:
        from .skills import skills_for_agent
        for sk in skills_for_agent(agent_id):
            for tid in sk.tool_ids:
                if tid in seen:
                    continue
                t = get_tool(tid)
                if t is not None:
                    tools.append(t)
                    seen.add(tid)
    except Exception:
        pass
    return tools


def _mock_pick_tool(agent_id: str, text: str) -> Optional[Tool]:
    tools = _agent_tools(agent_id)
    if not tools:
        return None
    t = text.lower()
    # Phase 6e — tracking / records intents take precedence (a citizen asking
    # "what's the status of GRV-…" must not trigger a NEW complaint).
    if _PROJECT_ID_RE.search(text) and "report" not in t and "issue" not in t \
            and "complain" not in t:
        if _ok(agent_id, "projects.track"):
            return get_tool("projects.track")
    if _RECORD_ID_RE.search(text):
        if any(w in t for w in ("remind", "reminder", "nudge", "follow up", "followup")):
            if _ok(agent_id, "records.send_reminder"):
                return get_tool("records.send_reminder")
        if any(w in t for w in ("status", "track", "where", "what happened",
                                "update", "என்ன ஆனது", "स्थिति", "ट्रैक")):
            if _ok(agent_id, "records.track"):
                return get_tool("records.track")
        if _ok(agent_id, "records.track"):
            return get_tool("records.track")

    # Phase 6f — ACTION intent must beat info intent. "My pension is pending for
    # 6 months, can I escalate?" was matching schemes.search (because of the word
    # "pension") and never registered anything. Escalation / complaint / ticket /
    # register / file / "pending too long" / "no response" are ACTIONS — they must
    # create a real, trackable record (which then shows in admin/ops), not return a
    # scheme list. These run BEFORE schemes.search.
    action_matchers: list[tuple[str, str]] = [
        ("records.list_mine",
         r"\bmy (complaints?|records?|grievances?|applications?|cases?)\b"
         r"|status of (all )?my\b|track all|list my"
         r"|meri (sab|saari )?shikayat|मेरी (सारी |सब )?शिकायत|என்(னுடைய)? புகார்கள்"),
        ("records.create",
         r"escalat|\bescalate\b"
         r"|raise (a |my |the |this )?(complaint|issue|grievance|matter|ticket)"
         r"|file (a |an |my )?(complaint|grievance|case|fir)"
         r"|register (a |my |this )?(complaint|grievance|case|issue)"
         r"|lodge (a )?(complaint|grievance)"
         r"|\bticket\b|grievance|complain"
         r"|pending (for|since)|been (waiting|pending)"
         r"|(no|koi) (response|update|reply|action|sunwai)"
         r"|darj kar|दर्ज कर|शिकायत|एस्केलेट|टिकट|सुनवाई नहीं|कोई जवाब नहीं"
         r"|புகார்|பதிவு செ|டிக்கெட்"
         # Phase 6g — agriculture complaints were slipping through: crop loss,
         # pest damage, compensation and insurance-claim asks are complaints too.
         r"|compensat|muavza|मुआवज|इल्तजा|crop (loss|damage|destroyed|failed|fail)"
         r"|(fasal|फसल).{0,12}(kharab|barbad|nasht|खराब|बर्बाद|नष्ट|नुकसान)"
         r"|पयिर|பயிர் (சேதம்|இழப்பு)|இழப்பீடு"
         r"|insurance claim|pmfby claim|बीमा (क्लेम|दावा)|காப்பீட்டு"),
    ]
    e6_matchers: list[tuple[str, str]] = [
        # Phase 6g — live mandi price queries (was unanswerable before).
        ("agriculture.get_mandi_price",
         r"\b(price|rate|bhav|mandi (rate|price)|market (price|rate)|msp)\b"
         r"|भाव|मंडी|कीमत|दाम|விலை|வில|ధర|ಬೆಲೆ|കിമത"),
        ("schemes.search",         r"\bschemes?\b|eligib|qualify|enroll|enrol|welfare|पात्र|योजना|திட்டம்|scholarship|pension|housing scheme|maternity|anganwadi|disability pension"),
        ("projects.find_near_me",  r"\bprojects? near|road work|construction near|development (work|project)|किस सड़क|சாலை வேலை"),
        ("projects.report_issue",  r"\b(pothole|road (work )?(abandoned|broken|bad)|गड्ढा|சாலை சேதம்)"),
    ]
    # Water emergencies are themselves complaints, so the specialised water tool
    # runs first (a "leak" stays a water complaint, not a generic record).
    water_matchers: list[tuple[str, str]] = [
        ("water.register_complaint",       r"\b(leak|no water|sewer|குழாய்|லீக்|पानी नहीं)"),
    ]
    # DigiLocker FETCH intents — only when the citizen wants their document, NOT
    # when they're complaining. So these run AFTER action intent.
    fetch_matchers: list[tuple[str, str]] = [
        ("digilocker.fetch_patta",         r"\bpatta|நிலம்|நிலத்தின்|भूमि|land record"),
        ("digilocker.fetch_ec",            r"\b(ec|encumbrance)\b"),
        ("digilocker.fetch_dl",            r"\b(licence|license|dl|driving)\b"),
        ("digilocker.fetch_ration_card",   r"\bration|राशन"),
    ]
    # Order: water-specific complaint -> ACTION intent (escalate/complaint/ticket
    # beats a bare "ration"/"patta" fetch) -> info (schemes/projects) -> fetch.
    for tid, pat in (water_matchers + action_matchers + e6_matchers + fetch_matchers):
        if re.search(pat, t, flags=re.UNICODE) and _ok(agent_id, tid):
            if (tid in ("records.create", "cmo.create_grievance")
                    and _is_advice_question(text)
                    and not _has_explicit_filing_request(text)):
                continue
            return get_tool(tid)

    # Drop-in plugins and MCP tools declare their own trigger keywords
    # (Tool.trigger_patterns) so they're reachable from chat without editing
    # this matcher. Built-in intents above keep priority; these are the
    # lowest-priority fallback and are already binding-filtered by
    # tools_for_agent().
    for tool in _agent_tools(agent_id):
        for pat in (getattr(tool, "trigger_patterns", None) or []):
            try:
                if re.search(pat, text, flags=re.UNICODE | re.IGNORECASE):
                    return tool
            except re.error:
                continue
    return None


def _ok(agent_id: str, tool_id: str) -> bool:
    # Honour the operator bindings (enable/disable + agent wiring) — same gate
    # the live LLM path uses — so the mock keyword matcher and the real engine
    # agree on what a tool is wired to. Falls back to in-code allowed_agents
    # for any tool that has no binding yet.
    return any(t.id == tool_id for t in _agent_tools(agent_id))


# ---------------------------------------------------------------------------
# Tool selection — real function-calling (model decides) with a keyword backstop
# ---------------------------------------------------------------------------

def _tool_schemas_for(agent_id: str) -> tuple[list[dict], dict[str, str]]:
    """Build OpenAI function schemas for the agent's wired tools.
    Returns (schemas, sanitized_name -> real_tool_id)."""
    schemas: list[dict] = []
    name_map: dict[str, str] = {}
    for t in _agent_tools(agent_id):
        sch = t.to_function_schema()
        clean = t.id.replace(".", _TOOL_NAME_SEP)
        sch.get("function", {})["name"] = clean
        name_map[clean] = t.id
        schemas.append(sch)
    return schemas, name_map


_TOOL_ROUTER_SYSTEM = (
    "You are the tool-routing step for a government-services assistant. "
    "Look at the citizen's latest message in context. If it can be acted on "
    "with one of the available tools, call exactly ONE tool, extracting the "
    "arguments from the conversation (e.g. a survey number, reference number, "
    "or crop name the citizen already gave). If no tool genuinely fits, do not "
    "call any tool. Never ask the citizen for details a tool does not require."
)


async def _llm_pick_tool(agent_id: str, agent_llm, conv_id: str,
                         latest_user_text: str) -> tuple[Optional[Tool], dict]:
    """Let the model choose a tool via function-calling. (tool, args) or (None, {})."""
    schemas, name_map = _tool_schemas_for(agent_id)
    if not schemas:
        return None, {}
    history = store.as_chat_messages(conv_id, limit=6)
    msgs = [{"role": "system", "content": _TOOL_ROUTER_SYSTEM}]
    msgs.extend(history)
    if not history or history[-1].get("role") != "user":
        msgs.append({"role": "user", "content": latest_user_text})
    try:
        out = await agent_llm.chat_with_tools(messages=msgs, tools=schemas)
    except Exception as e:  # noqa: BLE001 — degrade to keyword backstop
        log.warning("function-calling tool pick failed (%s); using keyword backstop", e)
        return None, {}
    calls = out.get("tool_calls") or []
    if not calls:
        return None, {}
    first = calls[0]
    tool_id = name_map.get(first.get("name")) or first.get("name")
    tool = get_tool(tool_id)
    if not tool or not _ok(agent_id, tool_id):
        return None, {}
    return tool, dict(first.get("arguments") or {})


async def _select_tool(agent_id: str, agent_llm, conv_id: str,
                       latest_user_text: str, channel: str,
                       voice: bool = False) -> tuple[Optional[Tool], dict]:
    """Hybrid selector: real function-calling first (model decides + extracts
    args), then the keyword matcher as a deterministic backstop for must-fire
    intents. Returns (tool, args).

    On VOICE turns the function-calling pre-pass is skipped: it adds a second
    Sarvam round-trip before the reply, which on a live call is audible dead
    air. The local keyword matcher adds zero latency and still catches the
    common helpline actions (register/track a record, check dues). Chat keeps
    full function-calling, where the extra hop is hidden behind the typing
    indicator."""
    if (_TOOL_FUNCTION_CALLING and not voice
            and getattr(agent_llm, "supports_tools", False)):
        tool, args = await _llm_pick_tool(agent_id, agent_llm, conv_id, latest_user_text)
        if tool is not None:
            args.setdefault("_channel", channel)
            return tool, args
    if _TOOL_DETECTION:
        kw = _mock_pick_tool(agent_id, latest_user_text)
        if kw is not None:
            return kw, _mock_tool_args(kw, agent_id, latest_user_text, channel)
    return None, {}


def _human_tool_summary(tool_id: str, result: dict) -> str:
    """A short, citizen-friendly one-liner for the tool_result card text
    (avoids dumping raw JSON into the chat)."""
    if not isinstance(result, dict):
        return "Done."
    if not result.get("ok", True):
        return result.get("message") or "Couldn't complete that — please try again."
    if tool_id in ("records.create", "cmo.create_grievance",
                   "water.register_complaint") and result.get("record_id"):
        return result.get("message") or f"Registered as {result['record_id']}."
    if tool_id == "records.track":
        rec = result.get("record") or {}
        rid = rec.get("recordId") or rec.get("record_id") or rec.get("reference") or ""
        st = rec.get("status") or ""
        lvl = rec.get("level") or rec.get("current_level") or ""
        if rid:
            return f"{rid}: {st}" + (f" (level {lvl})" if lvl else "")
        return result.get("message") or "Status checked."
    if tool_id == "records.list_mine":
        return f"You have {result.get('count', 0)} record(s)."
    if tool_id == "schemes.search":
        return f"Found {result.get('count', 0)} matching scheme(s)."
    if tool_id == "schemes.apply" and result.get("record_id"):
        return f"Application submitted ({result['record_id']})."
    if tool_id == "projects.track":
        p = result.get("project") or {}
        return f"{p.get('name', 'Project')}: {p.get('status', '')}".strip(": ")
    return result.get("message") or "Done."


# Phase 6f — fabricated reference patterns the LLM tends to invent, e.g.
# "G-2024-00567", "GRV/123/2024", "ticket no 4567". The OFFICIAL format is
# GRV-TN-2026-000123 (handled by _RECORD_ID_RE).
_FAKE_REF_RE = re.compile(
    r"\b(?:G|GRV|TKT|REF|TICKET|CASE)[-/ ]?\d{2,4}[-/ ]?\d{2,6}\b", re.I)


def _known_record_ids(citizen_id: str) -> set[str]:
    try:
        from .records.store import records_store
        return {r.record_id.upper() for r in records_store.for_citizen(citizen_id)}
    except Exception:
        return set()


def _tool_grounding_block(tool_ctx: Optional[tuple], lang: str) -> str:
    """Render the REAL tool output as a system instruction so the model states
    facts (the actual reference number / status) instead of inventing them."""
    if not tool_ctx:
        return ""
    tool, res = tool_ctx
    if not isinstance(res, dict):
        return ""
    tid = getattr(tool, "id", "")
    if tid in ("records.create", "cmo.create_grievance", "water.register_complaint",
               "schemes.apply", "projects.report_issue") and res.get("ok") and res.get("record_id"):
        rid = res["record_id"]
        desk = res.get("owner_desk", "the concerned desk")
        due = res.get("sla_due_at", "")
        dup = res.get("duplicate")
        kind = "application" if tid == "schemes.apply" else "complaint/escalation"
        docs = res.get("documents_required") or []
        docline = (f"documents_required: {', '.join(docs)}\n" if docs else "")
        return (
            "\n\n=== SYSTEM ACTION JUST COMPLETED — A REAL RECORD NOW EXISTS ===\n"
            f"reference_number: {rid}\nowner_desk: {desk}\nsla_due: {due}\n{docline}"
            + ("note: this matched the citizen's existing open case (not a new duplicate).\n" if dup else "")
            + f"INSTRUCTION: Confirm the {kind} is registered and give the citizen "
              f"EXACTLY this reference number — {rid} — and nothing else. Do NOT invent or alter "
              "the number. Say they can track it with this reference"
            + (", and tell them which documents to keep ready" if docs else "")
            + ". Cite the standard 30 working-day response time. 1-2 short sentences.")
    if tid == "records.track":
        if res.get("ok") and res.get("record"):
            return (
                "\n\n=== SYSTEM STATUS LOOKUP (report accurately, invent nothing) ===\n"
                f"{json.dumps(res['record'], ensure_ascii=False)[:600]}\n"
                "INSTRUCTION: Summarise the current status, level and next step in 1-2 sentences. "
                "Only mention a date if it appears in this data.")
        return (
            "\n\n=== SYSTEM STATUS LOOKUP: NO RECORD FOUND ===\n"
            "INSTRUCTION: Tell the citizen you couldn't find that reference, ask them to re-check "
            "it, and offer to register a fresh grievance. Do NOT invent a status or number.")
    if tid == "records.list_mine":
        return (
            "\n\n=== SYSTEM: THE CITIZEN'S OWN RECORDS (use these only) ===\n"
            f"count={res.get('count', 0)}; {json.dumps(res.get('records', []), ensure_ascii=False)[:600]}\n"
            "INSTRUCTION: List them briefly with their reference numbers and status. If none, say so plainly.")
    if tid == "schemes.search":
        schemes = res.get("schemes") or []
        slim = [{"name": s.get("name"), "benefit": s.get("benefit"),
                 "helpline": s.get("helpline")} for s in schemes[:4] if isinstance(s, dict)]
        if not slim:
            return (
                "\n\n=== SYSTEM: SCHEME SEARCH RETURNED NOTHING ===\n"
                "INSTRUCTION: Say you don't have a matching scheme on hand and ask one clarifying "
                "detail (what help they need), or point them to the department helpline. "
                "Do NOT invent a scheme, amount, or eligibility rule.")
        return (
            "\n\n=== SYSTEM: SCHEME SEARCH RESULTS (use ONLY these; never invent amounts) ===\n"
            f"{json.dumps(slim, ensure_ascii=False)[:600]}\n"
            "INSTRUCTION: Recommend the 1-2 most relevant. If they ask HOW TO APPLY, tell them the "
            "benefit and that you can register their application/interest now, and give the helpline "
            "for anything you don't have. Never invent an amount, eligibility rule, or deadline.")
    if tid == "schemes.check_eligibility":
        return (
            "\n\n=== SYSTEM: ELIGIBILITY RESULT (report exactly) ===\n"
            f"{json.dumps(res, ensure_ascii=False)[:500]}\n"
            "INSTRUCTION: Tell the citizen whether they appear eligible and the key reason. "
            "If a detail is missing, ask for that ONE detail. Do not guess.")
    if tid in ("projects.track", "projects.find_near_me"):
        key = "project" if tid == "projects.track" else "projects"
        return (
            "\n\n=== SYSTEM: PROJECT DATA (report exactly, invent nothing) ===\n"
            f"{json.dumps(res.get(key), ensure_ascii=False)[:600]}\n"
            "INSTRUCTION: Summarise the project(s), percent-complete and expected completion in "
            "1-2 sentences. If none found, say so plainly.")

    # Generic grounding for any other tool — drop-in plugins, MCP tools, and
    # DigiLocker fetches. WITHOUT this the model never sees the tool's output
    # and may ask the citizen for details the tool already answered (e.g. it
    # asks "which city?" even though the dues lookup already returned the
    # amount). This is what makes a freshly added tool usable end-to-end.
    if res.get("ok", True):
        summary = res.get("message")
        slim = {k: v for k, v in res.items() if k not in ("ok", "is_mock", "message")}
        return (
            f"\n\n=== SYSTEM: TOOL '{tid}' JUST RAN — USE ITS RESULT ===\n"
            f"{json.dumps(slim, ensure_ascii=False)[:700]}\n"
            + (f"summary: {summary}\n" if summary else "")
            + "INSTRUCTION: Answer the citizen's question DIRECTLY using these values. "
              "The tool has already returned the answer — do NOT ask for any further "
              "details it did not require (such as city, district or municipality). "
              "State the key facts in 1-2 short sentences; invent nothing beyond this data.")
    # Tool ran but reported a failure.
    err = res.get("message") or res.get("error") or "the lookup could not be completed"
    return (
        f"\n\n=== SYSTEM: TOOL '{tid}' COULD NOT COMPLETE ===\n"
        f"detail: {json.dumps(err, ensure_ascii=False)[:300]}\n"
        "INSTRUCTION: Briefly tell the citizen it couldn't be completed right now. If the "
        "tool needs one specific input, ask for that ONE thing. Do not invent data.")


def _fix_fabricated_refs(text: str, citizen_id: str, real_id: str = "",
                         *, lang: str = "en-IN") -> str:
    """Replace any made-up reference/ticket number with the real one (if a
    record was created this turn) or remove it (so we never quote a number
    that isn't in the system). Real, existing record ids are left untouched."""
    if not text:
        return text
    known = _known_record_ids(citizen_id)
    if real_id:
        known.add(real_id.upper())

    def _repl(m: "re.Match") -> str:
        tok = m.group(0)
        if tok.upper() in known:
            return tok                      # a genuine id — keep it
        if real_id:
            return real_id                  # swap the fake for the real one
        return "__FAKEREF__"                # mark for sentence-level cleanup

    # Catch BOTH the official format (GRV-TN-2026-000123) that isn't a real id
    # (e.g. parroted from an example) AND obviously-invented formats (G-2024-...).
    cleaned = _RECORD_ID_RE.sub(_repl, text)
    cleaned = _FAKE_REF_RE.sub(_repl, cleaned)
    if "__FAKEREF__" not in cleaned:
        return cleaned

    # No real id to substitute: drop sentences that promised a fake number.
    honest = {
        "hi-IN": "मैं अभी इसे दर्ज कर रही हूँ — रेफरेंस नंबर मिलते ही मैं आपको बता दूँगी।",
        "ta-IN": "நான் இப்போது இதைப் பதிவு செய்கிறேன் — குறிப்பு எண் கிடைத்ததும் சொல்கிறேன்.",
        "bn-IN": "আমি এখন এটি নথিভুক্ত করছি — রেফারেন্স নম্বর পেলেই জানিয়ে দেব।",
        "mr-IN": "मी आत्ता हे नोंदवत आहे — संदर्भ क्रमांक मिळताच कळवते.",
    }.get(lang, "I'm registering this now — I'll share the reference number as soon as it's generated.")
    parts = re.split(r"(?<=[.!?।])\s+", cleaned)
    kept = [p for p in parts if "__FAKEREF__" not in p]
    out = " ".join(kept).strip()
    return (out + (" " if out else "") + honest).strip()


def _norm_for_dup(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\wऀ-෿]+", " ", (s or "").lower())).strip()


def _is_near_duplicate(a: str, b: str) -> bool:
    """True if two replies are essentially the same statement (so we don't
    say it twice on a call). Uses normalised exact-match plus a token
    Jaccard overlap so minor rewordings still count as repetition."""
    na, nb = _norm_for_dup(a), _norm_for_dup(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # very short replies: require exact match only (handled above)
    ta, tb = set(na.split()), set(nb.split())
    if len(ta) < 4 or len(tb) < 4:
        return False
    inter = len(ta & tb)
    union = len(ta | tb) or 1
    return (inter / union) >= 0.85


def _mock_tool_args(tool: Tool, agent_id: str, text: str, channel: str) -> dict:
    """Build appropriate args for a mock-picked tool from the user text."""
    base = {"text": text, "_channel": channel}
    tid = tool.id
    if tid in ("records.track", "records.send_reminder", "records.submit_feedback"):
        m = _RECORD_ID_RE.search(text)
        if m:
            base["record_id"] = m.group(1).upper()
        rm = re.search(r"\b([1-5])\s*(?:star|/5|out of 5)?\b", text)
        if rm:
            base["rating"] = int(rm.group(1))
        return base
    if tid == "projects.track":
        m = _PROJECT_ID_RE.search(text)
        if m:
            base["project_id"] = m.group(1).upper()
        return base
    if tid == "projects.report_issue":
        m = _PROJECT_ID_RE.search(text)
        if m:
            base["project_id"] = m.group(1).upper()
        base["category"] = "road_defect"
        return base
    if tid == "schemes.search":
        base["query"] = text
        return base
    if tid == "agriculture.get_mandi_price":
        base["commodity"] = _guess_commodity(text)
        return base
    if tid in ("records.create", "water.register_complaint", "cmo.create_grievance"):
        if agent_id == "water":
            base["category"] = _guess_water_cat(text)
        elif agent_id == "pwd":
            base["category"] = "road_defect"
        else:
            base["category"] = _guess_category(agent_id, text)
        base["department_id"] = agent_id
        base["title"] = text[:120]
        # Phase 6f — escalations and long-pending cases come in hot.
        tl = text.lower()
        if any(w in tl for w in ("escalat", "pending for", "pending since",
                                 "no response", "no update", "months",
                                 "एस्केलेट", "महीने", "सुनवाई नहीं")):
            base["priority"] = "high"
        # Phase 6g — urgent asks file at emergency priority.
        if _is_urgent(text):
            base["priority"] = "emergency"
        return base
    # Drop-in plugins / MCP tools: best-effort fill of their declared inputs
    # from the user text (the keyword path has no LLM to extract arguments).
    if getattr(tool, "source", "builtin") in ("plugin", "mcp"):
        props = (tool.input_schema or {}).get("properties", {}) or {}
        for pname in props:
            low = pname.lower()
            if low in ("survey_no", "survey_number", "survey"):
                m = re.search(r"survey\s*(?:number|no\.?|#)?\s*"
                              r"([0-9]+(?:[/-][0-9]+[A-Za-z]?)?)",
                              text, re.IGNORECASE)
                if m:
                    base[pname] = m.group(1)
            elif low == "ifsc":
                m = re.search(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", text, re.IGNORECASE)
                if m:
                    base[pname] = m.group(1).upper()
            elif low in ("query", "q", "text", "question", "search"):
                base[pname] = text
        return base
    return base


# Phase 6g — commodity extraction for the mandi-price tool. English + common
# Hindi/Tamil crop names mapped to the Agmarknet English commodity name.
_COMMODITY_WORDS: dict[str, str] = {
    "onion": "Onion", "pyaz": "Onion", "प्याज": "Onion", "வெங்காயம்": "Onion",
    "tomato": "Tomato", "टमाटर": "Tomato", "தக்காளி": "Tomato",
    "potato": "Potato", "aloo": "Potato", "आलू": "Potato", "உருளை": "Potato",
    "paddy": "Paddy", "rice": "Rice", "dhan": "Paddy", "धान": "Paddy",
    "चावल": "Rice", "நெல்": "Paddy", "அரிசி": "Rice",
    "wheat": "Wheat", "gehu": "Wheat", "gehun": "Wheat", "गेहूं": "Wheat", "गेहूँ": "Wheat",
    "maize": "Maize", "makka": "Maize", "मक्का": "Maize", "மக்காச்சோளம்": "Maize",
    "cotton": "Cotton", "kapas": "Cotton", "कपास": "Cotton", "பருத்தி": "Cotton",
    "sugarcane": "Sugarcane", "ganna": "Sugarcane", "गन्ना": "Sugarcane", "கரும்பு": "Sugarcane",
    "groundnut": "Groundnut", "moongfali": "Groundnut", "मूंगफली": "Groundnut",
    "soyabean": "Soyabean", "soybean": "Soyabean", "सोयाबीन": "Soyabean",
    "tur": "Arhar (Tur/Red Gram)", "arhar": "Arhar (Tur/Red Gram)", "अरहर": "Arhar (Tur/Red Gram)",
    "moong": "Green Gram (Moong)", "मूंग": "Green Gram (Moong)",
    "urad": "Black Gram (Urd Beans)", "उड़द": "Black Gram (Urd Beans)",
    "banana": "Banana", "केला": "Banana", "வாழை": "Banana",
    "turmeric": "Turmeric", "हल्दी": "Turmeric", "மஞ்சள்": "Turmeric",
    "chilli": "Dry Chillies", "mirch": "Dry Chillies", "मिर्च": "Dry Chillies", "மிளகாய்": "Dry Chillies",
}


def _guess_commodity(text: str) -> str:
    t = (text or "").lower()
    for word, commodity in _COMMODITY_WORDS.items():
        if word in t:
            return commodity
    return ""   # tool will ask which crop


def _guess_cmo_cat(text: str) -> str:
    """Categorise a CM-cell grievance so the desk + reference prefix make sense."""
    t = text.lower()
    if any(w in t for w in ("pension", "पेंशन", "ஓய்வூதியம்")):
        return "pension_delay"
    if any(w in t for w in ("scholarship", "छात्रवृत्ति", "scheme", "योजना", "application", "आवेदन")):
        return "application_delay"
    if any(w in t for w in ("ration", "राशन", "pds")):
        return "ration_grievance"
    if any(w in t for w in ("water", "पानी", "leak", "supply")):
        return "water_grievance"
    if any(w in t for w in ("road", "pothole", "सड़क", "गड्ढा")):
        return "roads_grievance"
    if any(w in t for w in ("land", "patta", "भूमि", "जमीन")):
        return "land_grievance"
    if any(w in t for w in ("corrupt", "bribe", "रिश्वत", "भ्रष्ट")):
        return "vigilance"
    return "general_grievance"


# Phase 6f — per-department default grievance category, used when no keyword in
# the text gives a more specific one. Keeps each agent's records meaningful in
# the admin/ops queue instead of everything being "general_grievance".
_DEPT_DEFAULT_CAT: dict[str, str] = {
    "cmo": "general_grievance",
    "health": "health_grievance",
    "revenue": "revenue_grievance",
    "transport": "transport_grievance",
    "ration": "ration_grievance",
    "agriculture": "agri_grievance",
    "housing": "housing_grievance",
    "wcd": "wcd_grievance",
    "social": "social_grievance",
    "pwd": "pwd_grievance",
}


def _guess_category(agent_id: str, text: str) -> str:
    """Department-aware category: try keyword hints first (shared across the
    CM cell taxonomy), then fall back to the department's own default."""
    cat = _guess_cmo_cat(text)
    if cat == "general_grievance":
        return _DEPT_DEFAULT_CAT.get(agent_id, "general_grievance")
    return cat


def _guess_water_cat(text: str) -> str:
    t = text.lower()
    if "leak" in t: return "leak"
    if "sewer" in t or "blocked" in t: return "sewer_blockage"
    if "low pressure" in t or "pressure" in t: return "low_pressure"
    if "no water" in t or "no supply" in t: return "no_supply"
    if "quality" in t or "dirty" in t: return "quality"
    return "no_supply"


async def _emit_refusal(citizen_id: str, agent_id: str, conv_id: str, *,
                        text: str, lang: str, channel: Channel,
                        speak_reply: bool, state_obj, persona: dict) -> None:
    """Emit an off-topic refusal as a normal agent message (text + optional
    TTS). Mirrors the tail of _run_agent_turn_impl so the refusal looks and
    behaves like any other reply across channels."""
    msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}", convId=conv_id, role="agent",
        type="text", text=text, lang=lang, timestamp=datetime.utcnow(),
        channel="system",
    )
    msg.extra = {"detectedLanguage": lang, "scopeBlocked": True,
                 "personaName": (persona or {}).get("persona_name")}
    if speak_reply:
        try:
            from .language import tts_language_for
            spoken = re.sub(r"\[SOURCE:[^\]]*\]", "", text)
            spoken = re.sub(r"\[\d+\]", "", spoken).strip()
            voice = (persona or {}).get("voice") or get_agent(agent_id).voice
            tts_lang = tts_language_for(lang, state_obj, reply_text=spoken)
            tts = await tts_synthesize(spoken or text,
                                       target_language_code=tts_lang, speaker=voice)
            if tts.audio_bytes:
                msg.audioUrl = await _save_audio_blob(tts.audio_bytes, tts.mime)
                msg.durationSec = tts.duration_s
            msg.extra = {**(msg.extra or {}), "ttsVoice": voice, "ttsLanguage": tts_lang}
        except Exception:
            log.warning("refusal TTS failed (non-fatal)", exc_info=True)
    store.append(msg)
    await dispatcher.dispatch(
        citizen_id=citizen_id,
        frame={"type": "agent_message", "convId": conv_id, "agentId": agent_id,
               "message": msg.model_dump(mode="json")},
        primary_channel=channel)
    await ws_manager.send_to_citizen(citizen_id, {
        "type": "agent_typing", "convId": conv_id, "agentId": agent_id,
        "isTyping": False})


async def _send_consent_request(
    citizen_id: str, agent_id: str, conv_id: str, tool: Tool,
    user_text: str, channel: Channel,
) -> None:
    purpose = (
        f"The {get_agent(agent_id).name} would like to fetch your "
        f"{tool.name.replace('Fetch ', '').replace(' from DigiLocker', '')} from DigiLocker "
        f"to help you with your query."
    )
    req = create_consent_request(
        citizen_id=citizen_id, agent_id=agent_id, tool_id=tool.id,
        scope=tool.consent_scope, purpose=purpose, ttl_seconds=300,
    )
    await ws_manager.send_to_citizen(citizen_id, {
        "type": "agent_typing", "convId": conv_id,
        "agentId": agent_id, "isTyping": False,
    })

    if channel == "twilio_wa":
        # Plain text prompt — citizen replies YES or NO
        prompt = (
            f"🔐 Permission required\n\n{purpose}\n\n"
            f"Reply YES to allow, or NO to deny. (This request expires in 5 minutes.)"
        )
        # Build a synthetic agent_message frame so the dispatcher renders it
        # consistently across channels.
        await dispatcher.dispatch(
            citizen_id=citizen_id,
            frame={
                "type": "agent_message", "convId": conv_id,
                "agentId": agent_id,
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "convId": conv_id, "role": "agent", "type": "text",
                    "text": prompt, "lang": "en-IN",
                    "timestamp": datetime.utcnow().isoformat(),
                    "channel": "system", "extra": {"isConsentPrompt": True},
                },
            },
            primary_channel=channel,
        )
        _PENDING_TEXT_CONSENT[citizen_id] = (req.request_id, tool.id, user_text)
    else:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "consent_request",
            "convId": conv_id, "agentId": agent_id,
            "requestId": req.request_id,
            "toolId": tool.id, "toolName": tool.name,
            "scope": tool.consent_scope, "purpose": purpose,
            "expiresAt": req.expires_at.isoformat() + "Z",
            "userText": user_text,
        })


async def resume_after_consent_decision(
    *, citizen_id: str, agent_id: str, conv_id: str,
    tool_id: str, decision: str, user_text: str,
    channel: Channel = "simulator",
) -> None:
    tool = get_tool(tool_id)
    if not tool:
        return
    if decision == "denied":
        sys_msg = Message(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            convId=conv_id, role="system", type="system_event",
            text=f"You denied consent for {tool.name}. Continuing without it.",
            timestamp=datetime.utcnow(), channel="system",
        )
        store.append(sys_msg)
        await dispatcher.dispatch(
            citizen_id=citizen_id,
            frame={"type": "agent_message", "convId": conv_id,
                   "agentId": agent_id, "message": sys_msg.model_dump(mode="json")},
            primary_channel=channel,
        )
        asyncio.create_task(_run_agent_turn(
            citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
            latest_user_text=user_text, channel=channel,
            skip_tool_detection=True,    # consent was denied, don't re-ask
        ))
        return

    await _execute_tool_and_append(citizen_id, conv_id, tool, args={}, channel=channel)
    asyncio.create_task(_run_agent_turn(
        citizen_id=citizen_id, agent_id=agent_id, conv_id=conv_id,
        latest_user_text=user_text, channel=channel,
        skip_tool_detection=True,    # don't re-trigger the same tool
    ))


async def _execute_tool_and_append(
    citizen_id: str, conv_id: str, tool: Tool, *,
    args: dict, channel: Channel = "simulator",
    return_result: bool = False,
):
    """Execute a tool, audit it, persist + dispatch a tool_result card.

    Phase 6f: `return_result=True` returns the result dict so the LangGraph
    tools node can feed it back to the model as a ToolMessage.
    """
    try:
        result = await tool.execute(args, citizen_id)
        ok = True
    except Exception as e:
        result = {"ok": False, "error": str(e)}
        ok = False

    # Phase 6: every tool invocation lands in the audit log
    from . import audit as _audit
    _audit.append_event(
        actor=citizen_id, action="tool.invoke",
        resource={"citizenId": citizen_id, "toolId": tool.id,
                  "connector": tool.connector,
                  "requiresConsent": tool.requires_consent},
        payload={"ok": ok, "args_keys": list(args.keys())},
    )

    msg = Message(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        convId=conv_id, role="system", type="tool_result",
        # Phase 6f — show a clean, human-readable summary instead of a raw
        # "[tool:...] {json}" blob. The full result still rides in extra.result
        # for the rich card + debugging.
        text=_human_tool_summary(tool.id, result),
        timestamp=datetime.utcnow(), channel="system",
        extra={"toolId": tool.id, "result": result},
    )
    store.append(msg)
    await dispatcher.dispatch(
        citizen_id=citizen_id,
        frame={"type": "agent_message", "convId": conv_id,
               "agentId": tool.allowed_agents[0] if tool.allowed_agents else "cmo",
               "message": msg.model_dump(mode="json")},
        primary_channel=channel,
    )
    if return_result:
        return result


# ---------------------------------------------------------------------------
# Audio storage
# ---------------------------------------------------------------------------

def _audio_dir() -> Path:
    d = Path(settings.data_dir) / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _save_audio_blob(blob: bytes, mime: str) -> str:
    if not blob:
        return ""
    ext = ".wav"
    if "webm" in mime: ext = ".webm"
    elif "ogg" in mime: ext = ".ogg"
    elif "mp4" in mime: ext = ".m4a"
    elif "mpeg" in mime: ext = ".mp3"
    name = f"a_{uuid.uuid4().hex[:14]}{ext}"
    p = _audio_dir() / name
    p.write_bytes(blob)
    return f"/api/v1/audio/{name}"


# ---------------------------------------------------------------------------
# Phase 6e — record notifier. The records.service layer calls this on every
# status change (escalation, reminder, feedback, close) so the citizen sees a
# live `record_update` frame + a system chat note in the owning department's
# conversation. Registered at import time below.
# ---------------------------------------------------------------------------

async def _record_notifier(record, event: str, message: str) -> None:
    citizen_id = record.citizen_id
    # 1. Structured frame for the "My Records" UI to update in place.
    try:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "record_update", "event": event,
            "record": record.public_view(),
        })
    except Exception:
        log.debug("record_update WS push failed for %s", record.record_id)
    # 2. A human-readable system note in the relevant conversation.
    try:
        conv_id = store.conv_id(citizen_id, record.department_id)
        store.get_or_create_conv(citizen_id, record.department_id)
        msg = Message(
            id=f"msg_{uuid.uuid4().hex[:12]}", convId=conv_id,
            role="system", type="system_event",
            text=message, timestamp=datetime.utcnow(), channel="system",
            extra={"recordId": record.record_id, "recordEvent": event,
                   "recordStatus": record.status, "recordLevel": record.current_level},
        )
        store.append(msg)
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "agent_message", "convId": conv_id,
            "agentId": record.department_id,
            "message": msg.model_dump(mode="json"),
        })
    except Exception:
        log.debug("record system note failed for %s", record.record_id)
    # 3. On RESOLVED, prompt the feedback card.
    if record.status == "RESOLVED":
        try:
            await ws_manager.send_to_citizen(citizen_id, {
                "type": "feedback_request", "recordId": record.record_id,
                "title": record.title, "department": record.department_id,
            })
        except Exception:
            pass


try:
    from .records import service as _rsvc
    _rsvc.set_notifier(_record_notifier)
    log.info("records notifier registered")
except Exception as _e:  # pragma: no cover
    log.warning("could not register records notifier: %s", _e)
