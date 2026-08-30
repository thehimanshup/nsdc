"""Builds the LangGraph StateGraph (the Brain + Team) — Phase 6f.

Flow:  START → pre_hooks → retrieve → agent → (tools → agent)* → post → END

Nodes reuse the proven building blocks (prompt-safety, RAG, personas, tools,
consent, audit, TTS, dispatch) so behaviour matches the legacy engine; the
graph supplies the *structure* (typed state, nodes, edges, checkpointer) and —
in live mode — real LLM function-calling via bound tools.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from functools import lru_cache

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from ..config import settings
from .state import AgentState
from . import llm_adapter, lc_tools, tool_adapter, tracing

log = logging.getLogger("graph.build")

# Cap attached-skill instruction blocks injected into the system prompt, so many
# wired skills can't unbound the prompt. Extra skills' tools still bind.
_MAX_SKILLS_IN_PROMPT = 4


def _lc_history(conv_id: str, limit: int = 8) -> list:
    """Convert stored chat history → LangChain messages."""
    from ..store import store
    out = []
    for m in store.as_chat_messages(conv_id, limit=limit):
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            out.append(AIMessage(content=m["content"]))
    return out


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_pre_hooks(state: AgentState) -> dict:
    """Layer 1 pre-hooks: fence input, flag injection, detect language."""
    from .. import prompt_safety as _ps, audit as _audit
    from ..language import resolve_turn_language
    from ..states import detect_state_from_text, get_state
    from ..store import store

    text = state["latest_user_text"]
    fenced = _ps.fence_user_input(text)
    flags = fenced.injection_hits if fenced.suspicious else []
    if fenced.suspicious:
        _audit.append_event(
            actor=state["citizen_id"], action="security.prompt_injection_attempt",
            resource={"citizenId": state["citizen_id"], "agentId": state["agent_id"],
                      "engine": "graph"},
            payload={"hits": flags, "preview": text[:160]})

    citizen = store.get_citizen(state["citizen_id"]) or {}
    stated = detect_state_from_text(text or "")
    if stated and stated.code != citizen.get("state_code"):
        store.set_citizen_state(state["citizen_id"], stated.code, stated.primary_language)
        citizen = store.get_citizen(state["citizen_id"]) or citizen
    sc = citizen.get("state_code", "") or state.get("state_code", "")
    sd = get_state(sc).primary_language if (sc and get_state(sc)) else "en-IN"
    lang = resolve_turn_language(
        text or "",
        current_lang=citizen.get("language", sd),
        state_default=sd,
    )
    if lang and lang != citizen.get("language"):
        store.set_citizen_language(state["citizen_id"], lang)

    prior = sum(1 for m in store.conversations.get(state["conv_id"], [])
                if m.role == "agent")
    return {"fenced_text": fenced.fenced_text, "injection_flags": flags,
            "lang": lang, "state_code": sc, "prior_agent_replies": prior}


async def node_scope(state: AgentState) -> dict:
    """Layer 1 topical-scope gate. If the citizen turn is clearly off-topic
    (joke / puzzle / code / roleplay / …) short-circuit with a warm in-language
    refusal instead of letting the agent LLM answer it. Routes straight to the
    post node (which persists + dispatches + voices the reply)."""
    if not settings.scope_guard_enabled:
        return {}
    from .. import scope_guard, audit as _audit
    hist = None
    if state.get("prior_agent_replies"):
        from ..store import store
        hist = store.as_chat_messages(state["conv_id"], limit=6)
    verdict = await scope_guard.check(state["latest_user_text"],
                                      agent_id=state["agent_id"], history=hist)
    if verdict.in_scope:
        return {}
    _audit.append_event(
        actor=state["citizen_id"], action="security.off_topic_blocked",
        resource={"citizenId": state["citizen_id"], "agentId": state["agent_id"],
                  "engine": "graph"},
        payload={"category": verdict.category, "confidence": verdict.confidence,
                 "via": verdict.via, "preview": (state["latest_user_text"] or "")[:160]})
    text = await scope_guard.refusal(agent_id=state["agent_id"],
                                     lang=state.get("lang", "en-IN"),
                                     category=verdict.category)
    return {"scope_blocked": True, "final_text": text}


async def node_retrieve(state: AgentState) -> dict:
    """Knowledge layer: structured RAG with state filtering + cross-corpus."""
    from ..agents import get_agent
    from ..retrieval.pipeline import retrieve_with_meta
    agent = get_agent(state["agent_id"])
    if not agent:
        return {"rag_context": "", "citations": [], "rag_low_confidence": True}
    corpus = agent.corpus_id or agent.id
    cross = list(getattr(agent, "cross_corpus_read", []) or [])
    # Phase 7 — attached skills may add their own RAG corpus to the turn.
    try:
        from ..skills import skills_for_agent
        for s in skills_for_agent(state["agent_id"]):
            if s.corpus_id and s.corpus_id not in cross and s.corpus_id != corpus:
                cross.append(s.corpus_id)
    except Exception:
        pass
    hits = retrieve_with_meta(corpus, state["latest_user_text"], k=4,
                              extra_corpora=cross or None,
                              state_code=state.get("state_code", ""))
    top = hits[0][1] if hits else 0.0
    chunks = [c for c, _ in hits]
    return {
        "rag_context": "\n\n".join(c.to_context_block() for c in chunks) if chunks else "",
        "citations": [c.as_citation() for c in chunks],
        "rag_low_confidence": top < 0.5, "rag_top_score": round(top, 4),
    }


def _system_prompt(state: AgentState) -> str:
    from ..agents import get_agent
    from .. import personas as _personas, prompt_safety as _ps
    from ..language import system_prompt_language_instruction
    from ..states import get_state
    agent = get_agent(state["agent_id"])
    sc = state.get("state_code", "")
    state_obj = get_state(sc) if sc else None
    if state["prior_agent_replies"] == 0:
        continuity = ("Conversation state: FIRST exchange. Greet warmly in your own "
                      "words, then ask how you can help.")
    else:
        continuity = (f"Conversation state: you've ALREADY been talking with this citizen "
                      f"({state['prior_agent_replies']} replies). DO NOT greet again — just "
                      f"answer the latest question directly.")
    few_shot = _personas.render_few_shot_block(
        state["agent_id"], state["latest_user_text"], n=3,
        lang=state.get("lang", "en-IN"))
    safe_rag = _ps.neutralise_fence_sentinels(state.get("rag_context", ""))
    tone = _personas.channel_tone_block("voice" if state.get("speak_reply") else state["channel"])
    base = agent.system_prompt(rag_context=safe_rag, few_shot_block=few_shot,
                               conversation_continuity_block=continuity,
                               state_code=sc, voice_seed=state["conv_id"])
    lang_instr = system_prompt_language_instruction(
        state.get("lang", "en-IN"), sc,
        channel="voice" if state.get("speak_reply") else state["channel"])
    label = f"{state_obj.name} ({state_obj.code})" if state_obj else "India"
    try:
        from ..store import store as _store
        from ..conversation_quality import (
            detect_slot_loop, extract_known_facts, render_behavior_contract,
        )
        prior_msgs = _store.conversations.get(state["conv_id"], [])
        known_facts = extract_known_facts(prior_msgs, state.get("latest_user_text", ""))
        slot_loop = detect_slot_loop(prior_msgs, state.get("latest_user_text", ""))
        behavior = render_behavior_contract(
            channel=state["channel"], speak_reply=bool(state.get("speak_reply")),
            detected_lang=state.get("lang", "en-IN"), known_facts=known_facts,
            slot_loop=slot_loop,
        )
    except Exception:
        behavior = ""
    base += (f"\n\nCITIZEN CONTEXT:\n- state: {label}\n- language: {state.get('lang')}\n"
             f"- channel: {state['channel']}\n- {lang_instr}\n\n{tone}\n\n{behavior}")
    # Phase 6f — cross-agent coordinator: if this citizen is mid-workflow and
    # we're the current step's department, inject the step instructions +
    # accumulated shared context (the supervisor hand-off, Layer 3).
    try:
        from ..coordinator import get_active as _coord_active
        import json as _j
        cs = _coord_active(state["citizen_id"])
        if cs and not cs.completed and cs.current_step and cs.current_step.agent_id == state["agent_id"]:
            step = cs.current_step
            base += (f"\n\nCROSS-AGENT COORDINATOR — you are step "
                     f"{cs.current_step_idx + 1}/{len(cs.recipe.steps)} of "
                     f"'{cs.recipe.title}'.\nYour role: {step.purpose}\n"
                     f"Step instructions: {step.inject_context}\n"
                     f"Shared context from earlier steps: "
                     f"{_j.dumps(cs.shared_context, ensure_ascii=False)[:600]}")
    except Exception:
        pass
    # Phase 7 — attached skills: inject each skill's instruction fragment so the
    # agent knows how to use the tools the skill brought. Capped to keep the
    # system prompt bounded; truncation is logged (no silent caps).
    try:
        from ..skills import skills_for_agent
        skills = skills_for_agent(state["agent_id"])
        if skills:
            shown, cap = skills[:_MAX_SKILLS_IN_PROMPT], _MAX_SKILLS_IN_PROMPT
            if len(skills) > cap:
                log.warning("agent %s has %d skills attached; injecting first %d",
                            state["agent_id"], len(skills), cap)
            blocks = [f"- {s.name}: {s.instructions}".rstrip(": ").rstrip()
                      for s in shown if s.instructions]
            if blocks:
                base += ("\n\nSKILLS — extra capabilities available to you this "
                         "turn:\n" + "\n".join(blocks))
    except Exception:
        pass
    if state.get("rag_low_confidence"):
        base += ("\n\nIMPORTANT — low-confidence retrieval. Do NOT invent scheme amounts, "
                 "eligibility, or dates. If unsure, say so and point to the helpline.")
    return _ps.augment_system_prompt(base, agent.name)


async def node_agent(state: AgentState) -> dict:
    """Layer 2/3: the department subagent. Emits a tool call or a final reply."""
    from ..agents import get_agent
    from ..ws_manager import ws_manager
    agent = get_agent(state["agent_id"])
    await ws_manager.send_to_citizen(state["citizen_id"], {
        "type": "agent_typing", "convId": state["conv_id"],
        "agentId": state["agent_id"], "isTyping": True})

    lc_tools.set_turn_context(state["citizen_id"], state["conv_id"],
                              state["channel"], state["agent_id"])
    tools = lc_tools.langchain_tools_for_agent(state["agent_id"])
    model = llm_adapter.build_chat_model(
        state["agent_id"], state["channel"], getattr(agent, "llm_provider", None))
    if tools:
        model = model.bind_tools(tools)

    # Build the message list. On the first hop, seed from stored history with
    # the latest user turn fenced; on tool-loop hops, `messages` already holds
    # the running exchange (AIMessage tool call + ToolMessage).
    msgs = [SystemMessage(content=_system_prompt(state))]
    if state.get("messages"):
        msgs += list(state["messages"])
    else:
        hist = _lc_history(state["conv_id"], limit=8)
        if hist and isinstance(hist[-1], HumanMessage):
            hist[-1] = HumanMessage(content=state.get("fenced_text") or state["latest_user_text"])
        else:
            hist.append(HumanMessage(content=state.get("fenced_text") or state["latest_user_text"]))
        msgs += hist

    # Stream tokens → agent_token WS frames (parity with the legacy engine).
    # We accumulate AIMessageChunks so tool-calls are aggregated correctly.
    server_msg_id = state.get("server_msg_id")
    resp = None
    first_token = True
    try:
        async for chunk in model.astream(msgs):
            resp = chunk if resp is None else resp + chunk
            delta = getattr(chunk, "content", "") or ""
            if delta:
                if first_token:
                    await ws_manager.send_to_citizen(state["citizen_id"], {
                        "type": "agent_typing", "convId": state["conv_id"],
                        "agentId": state["agent_id"], "isTyping": False})
                    first_token = False
                await ws_manager.send_to_citizen(state["citizen_id"], {
                    "type": "agent_token", "convId": state["conv_id"],
                    "agentId": state["agent_id"], "serverMsgId": server_msg_id,
                    "delta": delta})
    except Exception as e:
        log.warning("graph agent stream error: %s", e)
    if resp is None:
        resp = AIMessage(content="Sorry, I hit a snag on my side. Please try again.")

    tool_calls = getattr(resp, "tool_calls", None) or []
    # Normalise a streamed AIMessageChunk into a plain AIMessage for state.
    final_msg = AIMessage(content=resp.content if isinstance(resp.content, str) else str(resp.content),
                          tool_calls=tool_calls)
    update = {"messages": [final_msg]}
    if tool_calls:
        update["tool_calls"] = tool_calls
    else:
        update["final_text"] = resp.content if isinstance(resp.content, str) else str(resp.content)
    return update


async def node_consent(state: AgentState) -> dict:
    """Layer 1 consent gate — the seam between the model's tool request and
    execution.

    B1 behaviour (Phase 7): if any pending tool requires consent, pause the turn
    and emit a consent request; the existing `/consent decide → resume` path
    (legacy engine) completes the turn once the citizen allows/denies. Milestone
    6 swaps this branch for LangGraph's native `interrupt()` once a durable
    checkpointer is in place. We check ALL pending calls up-front so nothing
    executes until consent is resolved.
    """
    from ..orchestrator import _send_consent_request
    for call in state.get("tool_calls", []):
        tool = tool_adapter.resolve(call["name"])
        if tool is not None and tool.requires_consent:
            await _send_consent_request(state["citizen_id"], state["agent_id"],
                                        state["conv_id"], tool,
                                        state["latest_user_text"], state["channel"])
            return {"consent_pending": True}
    return {}


async def node_tools(state: AgentState) -> dict:
    """Layer 4: execute approved tool calls (consent already cleared upstream).

    Each call runs through the agent's LangChain `StructuredTool` whose coroutine
    routes to `_execute_tool_and_append` (audit + PII + persist + dispatch) and
    re-attaches the implicit `_channel`/`agent_id` args from the per-turn context.
    """
    lc_tools.set_turn_context(state["citizen_id"], state["conv_id"],
                              state["channel"], state["agent_id"])
    by_name = {t.name: t for t in lc_tools.langchain_tools_for_agent(state["agent_id"])}
    new_msgs = []
    for call in state.get("tool_calls", []):
        lc = by_name.get(call["name"])
        if lc is None:
            new_msgs.append(ToolMessage(content='{"ok": false, "error": "unknown_tool"}',
                                        tool_call_id=call.get("id", "x")))
            continue
        result = await lc.coroutine(**(call.get("args") or {}))
        if isinstance(result, dict) and result.get("record_id"):
            state["last_record_id"] = result["record_id"]
        new_msgs.append(ToolMessage(
            content=json.dumps(result, ensure_ascii=False)[:1500],
            tool_call_id=call.get("id", "x")))
    return {"messages": new_msgs, "tool_calls": []}


async def node_post(state: AgentState) -> dict:
    """Layer 1 post-hooks: clean output, persist, dispatch, voice."""
    from ..agents import get_agent
    from ..channel_dispatcher import dispatcher
    from ..ws_manager import ws_manager
    from ..store import store
    from ..models import Message
    from .. import prompt_safety as _ps
    from ..orchestrator import _is_near_duplicate

    agent = get_agent(state["agent_id"])
    text = (state.get("final_text") or "").strip()
    persona = agent.resolve_persona(state.get("state_code", ""),
                                     voice_seed=state["conv_id"]) if agent else {}
    if not text:
        text = persona.get("signature_opener") or "How can I help you today?"

    # Output guardrails — identical helpers to the legacy engine.
    fb = persona.get("signature_opener") or "Sorry, could you say that again?"
    leak = _ps.scan_output_for_leakage(text, fallback=fb)
    if not leak.ok:
        text = leak.safe_text
    reasoning = _ps.detect_and_strip_reasoning(text, fallback=fb)
    if reasoning.leaked:
        text = reasoning.cleaned_text
    try:
        from ..conversation_quality import detect_slot_loop, postprocess_citizen_reply
        prior_msgs_for_nfr = store.conversations.get(state["conv_id"], [])
        slot_loop = detect_slot_loop(prior_msgs_for_nfr, state.get("latest_user_text", ""))
        text = postprocess_citizen_reply(text, slot_loop, lang=state.get("lang", "en-IN"))
    except Exception:
        pass
    # Romanised Indic -> native script (parity with the legacy engine). If the
    # model replied in Romanised Hindi/Tamil/Gujarati/etc. ("aapko jaana hoga"),
    # transliterate to native script so chat is readable AND Bulbul TTS speaks it
    # naturally. We also repoint state["lang"] so msg.lang + TTS use that language.
    try:
        from ..language import detect_romanised_indic, transliterate_to_native
        rom_lang = detect_romanised_indic(text)
        if rom_lang:
            converted = await transliterate_to_native(text, rom_lang)
            if converted and converted != text:
                log.warning("graph reply Romanised %s -> native script", rom_lang)
                text = converted
                state["lang"] = rom_lang
    except Exception:
        pass
    try:
        from ..language import enforce_reply_language
        corrected = await enforce_reply_language(text, state.get("lang", "en-IN"))
        if corrected and corrected != text:
            log.warning("graph reply language drift corrected to %s: %r",
                        state.get("lang", "en-IN"), text[:120])
            text = corrected
    except Exception:
        pass
    # Anti-repetition (voice + chat)
    prior = next((m.text for m in reversed(store.conversations.get(state["conv_id"], []))
                  if m.role == "agent" and (m.text or "").strip()), "")
    if prior and _is_near_duplicate(text, prior) and (state.get("lang") or "en").startswith("en"):
        text = "Is there anything else I can help you with?"

    msg = Message(id=state.get("server_msg_id") or f"msg_{uuid.uuid4().hex[:12]}",
                  convId=state["conv_id"], role="agent", type="text", text=text,
                  lang=state.get("lang", "en-IN"), timestamp=datetime.utcnow(),
                  channel="system")
    if state.get("citations"):
        msg.extra = {"citations": state["citations"],
                     "ragTopScore": state.get("rag_top_score", 0.0),
                     "ragLowConfidence": state.get("rag_low_confidence", False),
                     "engine": "graph"}
    else:
        msg.extra = {"engine": "graph"}

    # Voice synthesis (reuse the legacy TTS path)
    if state.get("speak_reply"):
        try:
            from ..voice import tts_synthesize
            from ..language import tts_language_for
            from ..states import get_state
            from ..orchestrator import _save_audio_blob
            so = get_state(state.get("state_code", "")) if state.get("state_code") else None
            voice = persona.get("voice") or (agent.voice if agent else "shubh")
            tts_lang = tts_language_for(state.get("lang", "en-IN"), so, reply_text=text)
            tts = await tts_synthesize(text, target_language_code=tts_lang, speaker=voice)
            msg.audioUrl = await _save_audio_blob(tts.audio_bytes, tts.mime)
            msg.durationSec = tts.duration_s
            msg.extra = {**(msg.extra or {}), "ttsVoice": voice, "ttsLanguage": tts_lang}
        except Exception as e:
            log.warning("graph TTS failed: %s", e)

    store.append(msg)
    await dispatcher.dispatch(
        citizen_id=state["citizen_id"],
        frame={"type": "agent_message", "convId": state["conv_id"],
               "agentId": state["agent_id"], "message": msg.model_dump(mode="json")},
        primary_channel=state["channel"])
    await ws_manager.send_to_citizen(state["citizen_id"], {
        "type": "agent_typing", "convId": state["conv_id"],
        "agentId": state["agent_id"], "isTyping": False})

    # Phase 6f — advance the cross-agent coordinator (Layer 3 hand-off). Skip
    # when this turn was an off-topic refusal (it isn't a workflow step).
    if not state.get("scope_blocked"):
        await _advance_coordinator(state)
    return {"final_text": text}


async def _advance_coordinator(state: AgentState) -> None:
    """If a coordinator workflow is active and we just ran its current step,
    mark the step done, push a progress frame, and (on completion) a summary —
    identical to the legacy engine's coordinator tail."""
    from ..coordinator import get_active as _coord_active, advance as _coord_advance
    from ..ws_manager import ws_manager
    from ..channel_dispatcher import dispatcher
    from ..store import store
    from ..models import Message
    cs = _coord_active(state["citizen_id"])
    if not (cs and cs.current_step and cs.current_step.agent_id == state["agent_id"]):
        return
    contribution = {f"step_{cs.current_step_idx}_agent": state["agent_id"],
                    f"step_{cs.current_step_idx}_reply_id": state.get("server_msg_id")}
    new_state = _coord_advance(cs.session_id, contribution=contribution)
    if not new_state:
        return
    await ws_manager.send_to_citizen(state["citizen_id"],
                                     {"type": "coordinator_state", "progress": new_state.progress()})
    if new_state.completed:
        done = Message(id=f"msg_{uuid.uuid4().hex[:12]}", convId=state["conv_id"],
                       role="system", type="system_event",
                       text=(f"✅ {new_state.recipe.title} complete — "
                             f"{len(new_state.recipe.steps)} agents collaborated. "
                             f"You'll receive updates as this progresses."),
                       timestamp=datetime.utcnow(), channel="system")
        store.append(done)
        await dispatcher.dispatch(
            citizen_id=state["citizen_id"],
            frame={"type": "agent_message", "convId": state["conv_id"],
                   "agentId": state["agent_id"], "message": done.model_dump(mode="json")},
            primary_channel=state["channel"])


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def _after_scope(state: AgentState) -> str:
    if state.get("scope_blocked"):
        return "post"        # off-topic — emit the refusal, skip retrieve/agent
    return "retrieve"


def _after_agent(state: AgentState) -> str:
    if state.get("tool_calls"):
        return "consent"     # gate before execution
    return "post"


def _after_consent(state: AgentState) -> str:
    if state.get("consent_pending"):
        return END           # turn paused; legacy resume completes it
    return "tools"


def _traced(name: str, fn):
    """Wrap a node in an OpenTelemetry span (no-op when tracing is off)."""
    async def wrapper(state: AgentState) -> dict:
        with tracing.span(f"graph.{name}", agent_id=state.get("agent_id"),
                          channel=state.get("channel")):
            return await fn(state)
    wrapper.__name__ = f"{name}_traced"
    return wrapper


@lru_cache(maxsize=1)
def get_graph():
    """Compile the graph once (cached). MemorySaver checkpointer = the durable
    Blackboard (swap to a Postgres checkpointer in Phase 7)."""
    tracing.init_tracing()
    g = StateGraph(AgentState)
    g.add_node("pre_hooks", _traced("pre_hooks", node_pre_hooks))
    g.add_node("scope", _traced("scope", node_scope))
    g.add_node("retrieve", _traced("retrieve", node_retrieve))
    g.add_node("agent", _traced("agent", node_agent))
    g.add_node("consent", _traced("consent", node_consent))
    g.add_node("tools", _traced("tools", node_tools))
    g.add_node("post", _traced("post", node_post))
    g.add_edge(START, "pre_hooks")
    g.add_edge("pre_hooks", "scope")
    g.add_conditional_edges("scope", _after_scope, {"retrieve": "retrieve", "post": "post"})
    g.add_edge("retrieve", "agent")
    g.add_conditional_edges("agent", _after_agent, {"consent": "consent", "post": "post"})
    g.add_conditional_edges("consent", _after_consent, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    g.add_edge("post", END)
    return g.compile(checkpointer=MemorySaver())
