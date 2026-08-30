"""The Blackboard — LangGraph shared state (Agentic Design Pattern, Layer 2).

A single typed state object threaded through every node. This is the
"Shared Context Space (Unified State)" from the diagram.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # identity / channel
    citizen_id: str
    agent_id: str            # active department subagent
    channel: str             # simulator | twilio_wa | livekit_app | ...
    conv_id: str
    state_code: str
    lang: str
    speak_reply: bool

    # conversation (LangChain messages; add_messages reducer appends)
    messages: Annotated[list, add_messages]
    latest_user_text: str

    # knowledge (retrieval)
    rag_context: str
    citations: list
    rag_low_confidence: bool
    rag_top_score: float

    # safety / guardrails
    injection_flags: list
    fenced_text: str
    scope_blocked: bool              # turn was off-topic; emit a refusal

    # tools / consent
    tool_calls: list                 # pending tool calls from the model
    consent_pending: bool            # turn paused awaiting consent
    last_record_id: Optional[str]

    # output
    final_text: str
    used_fallback: bool

    # bookkeeping
    prior_agent_replies: int
    server_msg_id: str
    extra: dict[str, Any]
