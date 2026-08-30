"""LiveKit Agent worker — separate process for real-time voice calls.

This file is a *separate* runtime from the main FastAPI backend. When
LiveKit is configured (LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET),
run this worker in a second terminal:

    python -m backend.livekit_agent_worker dev

Architecture:

    Citizen browser ─WebRTC─► LiveKit Room ◄─ joins ─► this Worker
                                              runs Sarvam pipeline:
                                                silero VAD
                                                Saaras V3 STT
                                                Sarvam-30B chat (with our RAG)
                                                Bulbul V3 TTS

The worker reads the room metadata to figure out which department agent
to embody. The main backend writes that metadata when minting the
LiveKit token via POST /api/v1/calls.

Dependencies (install only if you want LIVE voice calls):
    pip install "livekit-agents[sarvam]~=1.5" livekit-plugins-silero
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

# Phase 6f — CORPORATE-CA SSL FIX. On networks like HCL's, an enterprise proxy
# injects its own root certificate that isn't in Python's bundled `certifi`
# store, so the LiveKit Sarvam *LLM* plugin (which uses its own httpx/openai
# client) fails with "CERTIFICATE_VERIFY_FAILED" and the agent can hear you but
# can't think. truststore.inject_into_ssl() makes the standard `ssl` module use
# the OS certificate store (which DOES contain the corporate CA), fixing TLS for
# every library in this process. Must run BEFORE any HTTPS client is created, so
# it's at module import (which also covers the spawned job subprocess).
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
    print("[livekit-worker] truststore: using OS certificate store for TLS")
except Exception as _e:
    print(f"[livekit-worker] truststore not active ({_e!r}); "
          f"if you see CERTIFICATE_VERIFY_FAILED, run: pip install truststore")

# These imports are guarded — the script tolerates running with the
# livekit-agents package absent, in which case it prints setup instructions.
try:
    from livekit import rtc
    from livekit.agents import (
        Agent, AgentSession, JobContext, WorkerOptions, cli,
        RoomInputOptions, RoomOutputOptions,
    )
    # Phase 6g — function tools so live calls can ACT (register complaints,
    # track records) instead of hallucinating reference numbers.
    try:
        from livekit.agents import function_tool, RunContext
    except ImportError:  # older layouts
        from livekit.agents.llm import function_tool
        from livekit.agents.voice import RunContext
    from livekit.plugins import sarvam, silero
    _LIVEKIT_AVAILABLE = True
except ImportError as e:
    _LIVEKIT_AVAILABLE = False
    _IMPORT_ERR = e

    # No-op stand-ins so the module still imports (the class body below
    # references these at definition time even without livekit installed).
    def function_tool(f=None, **_kw):  # type: ignore
        if f is None:
            return lambda g: g
        return f

    class RunContext:  # type: ignore
        pass

# Reuse the same agent definitions + RAG + tools as the main backend
from .agents import AGENTS, get_agent
from .config import settings
from .rag import load_corpora, retrieve
from .conversation_quality import render_behavior_contract, SlotLoopState
from .language import (system_prompt_language_instruction,
                       resolve_turn_language)
from .states import BULBUL_TTS_LANGUAGES
from . import personas as _personas
from .personas import channel_tone_block


# ---------------------------------------------------------------------------
# Phase 6g — backend tool bridge.
#
# The worker is a separate process, so it must NOT write the JSON stores
# directly (the main backend keeps them in memory and would overwrite the
# file on its next persist — this is exactly why call-side complaints got a
# reference number the backend had never heard of). All tool calls are
# executed inside the main backend via POST /api/v1/internal/tools/execute.
# ---------------------------------------------------------------------------

_BACKEND_URL = os.environ.get(
    "VOICE_BACKEND_URL", f"http://127.0.0.1:{getattr(settings, 'port', 8000)}")
_INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")


async def _exec_backend_tool(agent_id: str, tool_id: str, args: dict,
                             msisdn: str | None, lang: str,
                             state_code: str) -> dict:
    """Run a backend tool in the MAIN backend process and return its result."""
    import httpx
    headers = {"x-internal-key": _INTERNAL_KEY} if _INTERNAL_KEY else {}
    payload = {
        "agent_id": agent_id, "tool_id": tool_id, "args": args,
        "msisdn": msisdn or "", "language": lang, "state_code": state_code,
        "channel": "livekit_app",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{_BACKEND_URL}/api/v1/internal/tools/execute",
                                  json=payload, headers=headers)
            r.raise_for_status()
            body = r.json()
            return body.get("result", body)
    except Exception as e:  # noqa: BLE001
        print(f"[livekit-worker] backend tool {tool_id} FAILED: {e!r}")
        return {"ok": False, "error": "backend_unreachable",
                "message": ("I could not register this in the system just now. "
                            "Apologise to the caller, do NOT invent a reference "
                            "number, and offer the department helpline instead.")}


# ---------------------------------------------------------------------------
# Phase 7 — DYNAMIC tools for voice. Instead of hardcoding @function_tool
# methods, ask the backend which tools this agent has (built-ins + drop-in
# plugins + MCP + skill-bundled tools — exactly what the CHAT agent sees) and
# expose each as a LiveKit raw-schema function tool. Execution is still
# forwarded to the backend (single writer process) via _exec_backend_tool.
# ---------------------------------------------------------------------------

def _make_raw_tool(agent_id: str, td: dict, msisdn: str | None,
                   lang: str, state: str):
    """Build one LiveKit raw-schema function tool from a backend tool def."""
    real_id = td["id"]
    clean = real_id.replace(".", "__")   # LLM function names can't contain dots
    schema = {
        "name": clean,
        "description": (td.get("description") or real_id)[:1024],
        "parameters": td.get("input_schema")
        or {"type": "object", "properties": {}, "required": []},
    }

    async def _handler(raw_arguments: dict) -> dict:
        return await _exec_backend_tool(agent_id, real_id,
                                        dict(raw_arguments or {}),
                                        msisdn, lang, state)
    try:
        _handler.__name__ = clean
    except Exception:
        pass
    return function_tool(_handler, raw_schema=schema)


async def _fetch_agent_tools(agent_id: str, msisdn: str | None, lang: str,
                             state: str) -> tuple[list, str]:
    """Ask the backend for this agent's tools + skill instructions and build
    LiveKit function tools. Returns (tools, skill_instructions). Degrades to
    ([], "") if the backend is unreachable — the call still works, just without
    tools."""
    import httpx
    headers = {"x-internal-key": _INTERNAL_KEY} if _INTERNAL_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{_BACKEND_URL}/api/v1/internal/tools/for-agent",
                json={"agent_id": agent_id}, headers=headers)
            r.raise_for_status()
            body = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[livekit-worker] could not fetch agent tools: {e!r}")
        return [], ""
    lk_tools = []
    for td in (body.get("tools") or []):
        try:
            lk_tools.append(_make_raw_tool(agent_id, td, msisdn, lang, state))
        except Exception as e:  # noqa: BLE001 — one bad schema mustn't kill the call
            print(f"[livekit-worker] skip tool {td.get('id')}: {e!r}")
    print(f"[livekit-worker] loaded {len(lk_tools)} dynamic tool(s) for "
          f"{agent_id}: {[t.info.name for t in lk_tools]}")
    return lk_tools, body.get("skill_instructions") or ""


def _state_from_lang(citizen_lang: str) -> str:
    """Best-effort state code from the call's language so the right named
    officer persona (Senthil/Priya/Suresh…) and matching Bulbul voice are
    picked. Falls back to TN (the app's home state)."""
    return {
        "ta-IN": "TN", "hi-IN": "UP", "bn-IN": "WB", "mr-IN": "MH",
        "kn-IN": "KA", "te-IN": "AP", "gu-IN": "GJ", "pa-IN": "PB",
    }.get((citizen_lang or "").strip(), "TN")


# Bulbul v3 female speaker names — used to tell the LLM the officer's gender so
# it uses the right gendered verb forms in Hindi/Marathi/Gujarati/Punjabi.
_FEMALE_VOICES = {
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali", "amelia",
    "sophia", "anushka", "vidya", "arya", "diya", "anjali",
}


def _voice_gender(voice: str) -> str:
    return "female" if (voice or "").strip().lower() in _FEMALE_VOICES else "male"


def _gender_instruction(gender: str, persona_name: str) -> str:
    """Tell the model the officer's gender so gendered languages sound right."""
    who = (persona_name or "").split(",")[0].strip() or "the officer"
    if gender == "female":
        return (
            f"YOUR GENDER: You are {who}, a WOMAN. In gendered languages (Hindi, "
            f"Marathi, Gujarati, Punjabi) ALWAYS use FEMININE first-person verb forms — "
            f"say 'कर सकती हूँ', 'देख रही हूँ', 'बताती हूँ', 'करूँगी' (NOT 'सकता', 'रहा', "
            f"'बताता', 'करूँगा'). In Marathi use 'करते', 'सांगते' (feminine). Never use "
            f"masculine forms for yourself.")
    return (
        f"YOUR GENDER: You are {who}, a MAN. In gendered languages (Hindi, Marathi, "
        f"Gujarati, Punjabi) use MASCULINE first-person verb forms — 'कर सकता हूँ', "
        f"'देख रहा हूँ', 'बताता हूँ', 'करूँगा'. Never use feminine forms for yourself.")


# How the agent should pick its reply language on a live call (auto-detect).
_MULTILINGUAL_INSTRUCTION = (
    "LANGUAGE — MIRROR THE LATEST TURN: The caller may speak English or ANY Indian "
    "language, and may switch mid-call. Each time you reply, detect the language of the "
    "caller's MOST RECENT utterance and reply in THAT language, written in its native "
    "script (Devanagari for Hindi/Marathi, தமிழ் for Tamil, বাংলা for Bengali, తెలుగు for "
    "Telugu, etc.). The language of EARLIER turns is irrelevant — if the caller spoke "
    "Hindi before but just asked something in English, reply in English now (and vice "
    "versa). Never keep replying in the opening language out of habit. Mirror the caller "
    "every single turn. Keep scheme names, helplines, and reference numbers as-is.")


def _build_system_prompt(agent_id: str, citizen_msisdn: str | None,
                         citizen_lang: str = "en-IN", citizen_state: str = "",
                         voice_seed: str = "") -> str:
    """Build the live-call system prompt at full parity with the chat path.

    Phase 6f — previously the live worker shipped a thinner prompt than the
    chat path: it injected RAG but NOT the persona few-shot examples or the
    per-channel voice tone block, so the model fell back to verbose,
    list-heavy prose (the "Health sounds too detailed" complaint). We now
    inject the spoken-style VOICE example bank + the human-call tone block +
    a state-aware persona, exactly like a real call would."""
    agent = get_agent(agent_id)
    if not agent:
        return ""
    state_code = (citizen_state or "").upper() or _state_from_lang(citizen_lang)
    chunks = retrieve(agent.corpus_id or agent.id, agent.description, k=3)
    rag = "\n\n".join(c.to_context_block() for c in chunks)
    # Phase 6f — spoken-style few-shot pairs so the agent imitates a human on
    # the phone (the single biggest lever for sounding natural).
    few_shot = _personas.render_few_shot_block(
        agent_id, agent.description, n=3, voice=True, lang=citizen_lang)
    # Per-channel voice tone block (acknowledge-first, no lists/URLs, brevity).
    tone_block = channel_tone_block("voice")
    # Phase 6e — we greet the caller once via session.say() in on_enter, so
    # tell the model the call is ALREADY in progress. Without this the model
    # re-introduces itself on the first user turn → the caller hears the
    # greeting twice.
    continuity = (
        "Conversation state: you are already on a live phone call and have ALREADY "
        "greeted the caller. Do NOT greet again, do NOT re-introduce yourself. "
        "Just answer their question directly."
    )
    # Phase 6f — officer gender (for correct Hindi/Marathi verb forms) and
    # auto-detect/mirror the caller's language.
    persona = agent.resolve_persona(state_code, voice_seed=voice_seed)
    gender = _voice_gender(persona.get("voice") or agent.voice)
    gender_line = _gender_instruction(gender, persona.get("persona_name", ""))
    behavior = render_behavior_contract(
        channel="livekit_app", speak_reply=True, detected_lang=citizen_lang,
        known_facts={}, slot_loop=SlotLoopState(),
    )
    return agent.system_prompt(rag_context=rag, few_shot_block=few_shot,
                               conversation_continuity_block=continuity,
                               state_code=state_code,
                               voice_seed=voice_seed) + (
        f"\n\n{tone_block}\n"
        f"\n{gender_line}\n"
        f"\n{_MULTILINGUAL_INSTRUCTION}\n"
        f"\nCITIZEN CONTEXT:\n"
        f"- channel: livekit_app (voice call)\n"
        f"- msisdn (last 4): {citizen_msisdn[-4:] if citizen_msisdn else '(unknown)'}\n"
        f"\nIMPORTANT for a natural phone call:\n"
        f"- You are a real human officer on the line — warm, calm, competent.\n"
        f"- Open each turn by briefly ACKNOWLEDGING what the caller said, then help.\n"
        f"- Keep every reply to 1-2 short spoken sentences, then STOP so the caller can talk.\n"
        f"- Give ONE step at a time; never recite a numbered checklist out loud.\n"
        f"- NEVER repeat yourself or restate your previous sentence. Don't repeat a helpline "
        f"or reference number you already said.\n"
        f"- If the caller interrupts you, stop and listen.\n"
        f"- Speak naturally with contractions. No URLs, no markdown, no lists read aloud.\n"
        f"\nTOOLS (Phase 6g — you can now ACT, not just talk):\n"
        f"- To file a complaint/grievance, FIRST confirm the issue and location in one "
        f"sentence ('Shall I register this complaint about X at Y?'). On a yes, CALL the "
        f"register_complaint tool. NEVER claim a complaint is registered without calling it.\n"
        f"- NEVER invent or guess a reference number. Only speak a reference number that a "
        f"tool actually returned, and read it slowly in groups (GRV - TN - 2026 - 0001 23).\n"
        f"- To check status of an existing complaint, call track_complaint with the "
        f"caller's reference number. To list their complaints, call list_my_complaints.\n"
        f"- If a tool fails, say so honestly and give the helpline — no fake numbers.\n\n"
        f"{behavior}"
    )


# Bulbul TTS pace nudged by persona tone so empathetic desks sound unhurried
# and operational desks sound crisp — within a tight, natural range.
_PACE_BY_TONE: dict[str, float] = {
    "empathetic": 0.96,
    "warm-helpful": 1.0,
    "matter-of-fact": 1.05,
    "brisk": 1.06,
    "formal-procedural": 1.0,
}


def _opener_first_name(persona_name: str, fallback: str) -> str:
    """'Priya, CM Special Cell officer (Tamil Nadu)' -> 'Priya'."""
    n = (persona_name or "").split(",")[0].strip()
    return n or fallback


def _call_opener(agent_def, lang: str, persona_name: str = "") -> str:
    """Warm, human, NATIVE-SCRIPT opener naming the officer + department.

    Phase 6f — the Hindi/Tamil/Bengali strings were previously corrupted into
    literal '?????' (mojibake), so non-English callers heard gibberish. These
    are proper, gender-neutral greetings."""
    dept = agent_def.name
    who = _opener_first_name(persona_name, dept)
    l = (lang or "").strip()
    if l.startswith("hi"):
        return f"नमस्ते! मैं {who}, {dept} से। बताइए, मैं आपकी कैसे मदद करूँ?"
    if l.startswith("ta"):
        return f"வணக்கம்! நான் {who}, {dept} சார்பாக. சொல்லுங்கள், எப்படி உதவலாம்?"
    if l.startswith("bn"):
        return f"নমস্কার! আমি {who}, {dept} থেকে বলছি। বলুন, কীভাবে সাহায্য করতে পারি?"
    if l.startswith("mr"):
        return f"नमस्कार! मी {who}, {dept} मधून बोलत आहे. सांगा, मी कशी मदत करू?"
    if l.startswith("te"):
        return f"నమస్తే! నేను {who}, {dept} నుండి. చెప్పండి, నేను ఎలా సహాయం చేయగలను?"
    return f"Hello! {who} here from the {dept}. How can I help you today?"


class DeptVoiceAgent(Agent if _LIVEKIT_AVAILABLE else object):
    def __init__(self, agent_id: str, citizen_msisdn: str | None,
                 citizen_lang: str = "en-IN", citizen_state: str = "",
                 voice_seed: str = "", dynamic_tools: list | None = None,
                 skill_instructions: str = ""):
        agent_def = get_agent(agent_id)
        if not agent_def:
            raise ValueError(f"Unknown agent: {agent_id}")

        # Phase 6f — resolve the state-aware persona so the live call uses the
        # right named officer, the matching Bulbul voice, and a tone-tuned pace.
        # Prefer the citizen's ACTUAL state (from call metadata); only guess
        # from language if it's unknown.
        state_code = (citizen_state or "").upper() or _state_from_lang(citizen_lang)
        self._citizen_state = state_code
        persona = agent_def.resolve_persona(state_code, voice_seed=voice_seed)
        self._persona_name = persona.get("persona_name", "")
        persona_voice = persona.get("voice") or agent_def.voice
        pace = _PACE_BY_TONE.get(persona.get("tone") or agent_def.tone, 1.0)
        # Voice "thinking" model. Per Sarvam's guidance for the live voice
        # pipeline we default every agent to sarvam-105b (better reasoning);
        # its token latency is masked because the LiveKit pipeline STREAMS the
        # reply into Bulbul TTS as tokens arrive. Override with VOICE_LLM_MODEL
        # (e.g. sarvam-105b-32k for long context, or sarvam-30b for max speed),
        # and a per-agent llm_provider still wins.
        llm_model = os.environ.get("VOICE_LLM_MODEL", "sarvam-105b")
        prov = (agent_def.llm_provider or "").strip()
        if prov.startswith("sarvam-"):
            llm_model = prov

        # Phase 6f — pass the Sarvam key EXPLICITLY. LiveKit spawns each job in a
        # fresh subprocess (especially on Windows), so the SARVAM_API_KEY env var
        # set in main() doesn't always reach it — the plugins then crash with
        # "Sarvam API key is required". settings.sarvam_api_key is loaded from
        # .env at import, so it's reliable here.
        _sarvam_key = settings.sarvam_api_key or os.environ.get("SARVAM_API_KEY", "")

        base_instructions = _build_system_prompt(
            agent_id, citizen_msisdn, citizen_lang, state_code, voice_seed=voice_seed)
        # Phase 7 — append attached-skill instructions (fetched from the backend)
        # so the voice agent gets the same "how to use these tools" guidance the
        # chat agent does.
        if skill_instructions:
            base_instructions += skill_instructions
        super().__init__(
            instructions=base_instructions,
            # Phase 7 — dynamic tools (built-ins + plugins + MCP + skill tools)
            # built from the backend's tool list. Replaces the old hardcoded
            # @function_tool methods.
            tools=dynamic_tools or [],
            # Phase 6f — use silero VAD with DEFAULT settings. A custom
            # activation_threshold=0.5 was too aggressive and rejected normal
            # speech, so the agent received "0 chunks" and never responded.
            vad=silero.VAD.load(),
            # Phase 6f — AUTO-DETECT the caller's language. language="unknown"
            # lets Saaras detect whatever the caller speaks (English, Hindi,
            # Tamil, …) and transcribe in its native script, so the agent can
            # mirror it. No forced sample_rate / high_vad_sensitivity.
            stt=sarvam.STT(
                model="saaras:v3", mode="transcribe",
                language="unknown",
                api_key=_sarvam_key,
            ),
            llm=sarvam.LLM(
                model=llm_model,
                temperature=0.4,
                api_key=_sarvam_key,
                # don't set low max_tokens — Sarvam reasons internally
            ),
            tts=sarvam.TTS(
                model="bulbul:v3",
                # the Sarvam LiveKit plugin uses `speaker=`, not `voice=`
                speaker=persona_voice,
                api_key=_sarvam_key,
                target_language_code=citizen_lang,
                pace=pace,
            ),
        )
        self._agent_id = agent_id
        self._citizen_lang = citizen_lang
        self._citizen_msisdn = citizen_msisdn
        # Phase 6g — sticky per-call language for TTS mirroring (see
        # maybe_switch_language). Starts at the language the call was opened in.
        self._current_tts_lang = citizen_lang
        # Base system prompt, kept so a mid-call language switch can re-inject a
        # strong "reply ONLY in <lang> now" directive into the LLM (repointing
        # the TTS voice alone isn't enough — the model must also WRITE the reply
        # in the new language or Bulbul mispronounces it).
        self._base_instructions = base_instructions

    # -----------------------------------------------------------------
    # Phase 7 — backend tools are now DYNAMIC. They're built from the backend's
    # tool list in _fetch_agent_tools() and passed to super().__init__(tools=...)
    # — so the voice agent uses the SAME tools the chat agent does (built-ins +
    # drop-in plugins + MCP + skill-bundled tools), with no hardcoded list here.

    # -----------------------------------------------------------------
    # Phase 6g — per-turn language mirroring for TTS. The prompt already
    # tells the LLM to mirror the caller; this makes Bulbul follow along
    # (it was pinned to the call's opening language, so a mid-call switch
    # to Hindi was synthesised with the wrong target language).
    # -----------------------------------------------------------------

    def maybe_switch_language(self, transcript: str) -> None:
        if not transcript or len(transcript.strip()) < 3:
            return
        # Phase 6g+ — use the same CONFIDENT-SWITCH resolver as the chat path
        # (was detect_language_from_text, which fell back to the current
        # language for low-stopword English like "pension amount details", so
        # the call stayed in the opening language). Stay sticky only on
        # genuinely ambiguous tokens ("ok", "haan").
        detected = resolve_turn_language(
            transcript, current_lang=self._current_tts_lang,
            state_default=self._current_tts_lang)
        if (not detected or detected == self._current_tts_lang
                or detected not in BULBUL_TTS_LANGUAGES):
            return
        prev = self._current_tts_lang
        self._current_tts_lang = detected
        # 1. Re-point Bulbul TTS so the synthesised voice uses the new language.
        try:
            self.tts.update_options(target_language_code=detected)
            print(f"[livekit-worker] caller switched language {prev} -> {detected}; "
                  f"TTS updated")
        except Exception as e:  # plugin may not support live updates
            print(f"[livekit-worker] TTS language switch failed: {e!r}")
        # 2. Re-instruct the LLM so its REPLY TEXT switches too. Without this
        #    the model often keeps writing the opening language (conversation
        #    momentum), and the repointed TTS then mispronounces it.
        try:
            import asyncio as _aio
            _aio.create_task(self._reinstruct_language(detected))
        except Exception as e:
            print(f"[livekit-worker] could not schedule LLM re-instruction: {e!r}")

    async def _reinstruct_language(self, lang: str) -> None:
        """Append a strong, current-language directive to the live instructions
        so the model's next reply is written in the language the caller just
        switched to. Best-effort — guarded for livekit-agents API differences."""
        name = {
            "en-IN": "English", "hi-IN": "Hindi", "ta-IN": "Tamil",
            "te-IN": "Telugu", "kn-IN": "Kannada", "ml-IN": "Malayalam",
            "bn-IN": "Bengali", "mr-IN": "Marathi", "gu-IN": "Gujarati",
            "pa-IN": "Punjabi", "od-IN": "Odia", "ur-IN": "Urdu",
        }.get(lang, lang)
        directive = (
            f"\n\n[LANGUAGE UPDATE] The caller has just switched to {name}. "
            f"From now on reply ONLY in {name} (native script, not romanised), "
            f"every turn, until they switch again."
        )
        try:
            await self.update_instructions(self._base_instructions + directive)
            print(f"[livekit-worker] LLM re-instructed to reply in {name}")
        except Exception as e:
            print(f"[livekit-worker] update_instructions unavailable ({e!r}); "
                  f"relying on prompt-level mirroring")

    async def on_enter(self):
        # livekit-agents 1.5 calls on_enter() with NO args; the AgentSession is
        # available as self.session. Greet the caller with the native-script
        # opener. allow_interruptions lets the caller barge in over the greeting.
        agent_def = get_agent(self._agent_id)
        if not agent_def:
            return
        opener = _call_opener(agent_def, self._citizen_lang, self._persona_name)
        print(f"[livekit-worker] on_enter: speaking greeting ({len(opener)} chars): {opener[:60]!r}")
        try:
            try:
                handle = self.session.say(opener, allow_interruptions=True)
            except TypeError:
                handle = self.session.say(opener)
            if handle is not None and hasattr(handle, "__await__"):
                await handle
            print("[livekit-worker] on_enter: greeting dispatched OK")
        except Exception as e:
            print(f"[livekit-worker] on_enter: greeting FAILED: {e!r}")


async def entrypoint(ctx: JobContext):
    """Called once per call — i.e., once per LiveKit room a worker
    is dispatched to."""
    await ctx.connect()
    print(f"[livekit-worker] connected to room: {ctx.room.name}")

    # Phase 6f — the backend puts the call's {agent_id, language, state_code}
    # in the CITIZEN PARTICIPANT's token metadata (room metadata is empty). So
    # read room metadata first, then fall back to the joining participant's
    # metadata. Previously this was missing → every call defaulted to CMO.
    meta: dict = {}
    if ctx.room.metadata:
        try:
            meta = json.loads(ctx.room.metadata)
        except Exception:
            pass
    if not meta:
        try:
            participant = await ctx.wait_for_participant()
            if participant and participant.metadata:
                meta = json.loads(participant.metadata)
                print(f"[livekit-worker] read participant metadata: {meta}")
        except Exception as e:
            print(f"[livekit-worker] could not read participant metadata: {e!r}")

    agent_id = meta.get("agent_id") or "cmo"
    citizen_msisdn = meta.get("citizen_msisdn")
    citizen_lang = meta.get("language") or "en-IN"
    citizen_state = meta.get("state_code") or ""
    if agent_id not in AGENTS:
        print(f"[livekit-worker] unknown agent_id={agent_id}, defaulting to cmo")
        agent_id = "cmo"

    print(f"[livekit-worker] embodying agent: {agent_id} "
          f"({get_agent(agent_id).name}) · lang={citizen_lang} · state={citizen_state or '(ask)'}")

    # Phase 6f — match Sarvam's working cookbook example exactly: a bare
    # AgentSession() and session.start(agent=..., room=...) with NO room
    # input/output options. The STT/LLM/TTS/VAD all live on the Agent. Passing
    # the (now deprecated) empty RoomInputOptions()/RoomOutputOptions() was
    # detaching the caller's microphone input immediately ("sent 0 chunks"),
    # so the agent never heard the caller and never spoke.
    # Phase 7 — fetch this agent's dynamic tools + skill instructions from the
    # backend (built-ins + plugins + MCP + skill tools, minus consent-gated),
    # so the voice agent has the SAME capabilities as the chat agent.
    dyn_tools, skill_instr = await _fetch_agent_tools(
        agent_id, citizen_msisdn, citizen_lang, citizen_state)
    session = AgentSession()
    agent = DeptVoiceAgent(
        agent_id, citizen_msisdn, citizen_lang, citizen_state,
        voice_seed=ctx.room.name,
        dynamic_tools=dyn_tools, skill_instructions=skill_instr,
    )
    await session.start(agent=agent, room=ctx.room)

    # Phase 6g — mirror the caller's language in TTS turn by turn. Saaras
    # transcribes in native script, so script analysis on the final
    # transcript is a reliable signal for mid-call language switches.
    try:
        @session.on("user_input_transcribed")
        def _on_transcribed(ev):
            try:
                if getattr(ev, "is_final", True):
                    agent.maybe_switch_language(getattr(ev, "transcript", ""))
            except Exception as e:
                print(f"[livekit-worker] language-mirror hook error: {e!r}")
    except Exception as e:
        print(f"[livekit-worker] could not attach transcript hook: {e!r}")

    # IMPORTANT: do NOT call session.aclose() here. session.start() returns as
    # soon as the session is set up (it does not block for the call), so calling
    # aclose() immediately tore the call down ~2s in ("session closed
    # user_initiated", greeting cut off, STT got 0 chunks). Instead, keep the
    # entrypoint alive until the caller actually leaves the room.
    import asyncio as _aio
    _ended = _aio.Event()
    try:
        ctx.room.on("disconnected", lambda *args: _ended.set())
    except Exception:
        pass
    await _ended.wait()


def main():
    if not _LIVEKIT_AVAILABLE:
        print()
        print("LiveKit Agent worker dependencies are NOT installed.")
        print()
        print(f"Import error was: {_IMPORT_ERR}")
        print()
        print("To install:")
        print("    pip install 'livekit-agents[sarvam]~=1.5' livekit-plugins-silero")
        print()
        print("Then start this worker in a separate terminal:")
        print("    python -m backend.livekit_agent_worker dev")
        print()
        print("In the meantime, the main backend supports MOCK calls (press-to-talk).")
        print("No worker needed for those.")
        sys.exit(1)

    if not (settings.livekit_url and settings.livekit_api_key
            and settings.livekit_api_secret):
        print()
        print("LiveKit credentials missing. Set in .env:")
        print("  LIVEKIT_URL=wss://YOUR-PROJECT.livekit.cloud")
        print("  LIVEKIT_API_KEY=APIxxxxx")
        print("  LIVEKIT_API_SECRET=xxxxx")
        print()
        print("Get a free LiveKit Cloud project at https://cloud.livekit.io")
        print("(Or self-host LiveKit per the LIVEKIT_SETUP.md guide.)")
        sys.exit(1)

    # Load the corpus so retrieve() works in this process too
    load_corpora()
    # Phase 6f — this is a separate process, so load the persona example banks
    # here too (chat + spoken-style voice) or few-shot injection comes back empty.
    _personas.load_examples()
    _personas.load_voice_examples()

    # Pass credentials via env vars the livekit-agents library expects
    os.environ.setdefault("LIVEKIT_URL", settings.livekit_url)
    os.environ.setdefault("LIVEKIT_API_KEY", settings.livekit_api_key)
    os.environ.setdefault("LIVEKIT_API_SECRET", settings.livekit_api_secret)
    if settings.sarvam_api_key:
        os.environ.setdefault("SARVAM_API_KEY", settings.sarvam_api_key)

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
# Phase 6g — end of file
