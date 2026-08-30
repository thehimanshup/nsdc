"""Public entrypoint for the LangGraph engine — Phase 6f.

`run_turn` is what the orchestrator calls when ORCHESTRATOR_ENGINE=graph. It
builds the initial Blackboard state, invokes the compiled graph (threaded by
conv_id so the checkpointer persists state per conversation), and lets the
graph's post node do the persist + dispatch.

Wrapped so any failure clears the typing indicator and surfaces a safe
fallback — the chat never hangs.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

log = logging.getLogger("graph.runtime")


async def run_turn(*, citizen_id: str, agent_id: str, conv_id: str,
                   latest_user_text: str, speak_reply: bool = False,
                   channel: str = "simulator") -> None:
    from .build import get_graph
    from ..store import store

    citizen = store.get_citizen(citizen_id) or {}
    init = {
        "citizen_id": citizen_id, "agent_id": agent_id, "conv_id": conv_id,
        "channel": channel, "state_code": citizen.get("state_code", ""),
        "lang": citizen.get("language", "en-IN"), "speak_reply": speak_reply,
        "latest_user_text": latest_user_text,
        "messages": [], "tool_calls": [], "citations": [],
        "server_msg_id": f"msg_{uuid.uuid4().hex[:12]}",
    }
    cfg = {"configurable": {"thread_id": conv_id}, "recursion_limit": 12}
    try:
        graph = get_graph()
        await graph.ainvoke(init, cfg)
    except Exception as e:
        log.exception("graph run_turn failed for citizen=%s agent=%s: %s",
                      citizen_id, agent_id, e)
        await _fallback(citizen_id, agent_id, conv_id, channel)


async def _fallback(citizen_id, agent_id, conv_id, channel) -> None:
    from ..ws_manager import ws_manager
    from ..channel_dispatcher import dispatcher
    from ..store import store
    from ..models import Message
    try:
        await ws_manager.send_to_citizen(citizen_id, {
            "type": "agent_typing", "convId": conv_id,
            "agentId": agent_id, "isTyping": False})
        msg = Message(id=f"msg_{uuid.uuid4().hex[:12]}", convId=conv_id,
                      role="agent", type="text",
                      text="Sorry, I hit a snag on my side. Please try again in a moment.",
                      lang="en-IN", timestamp=datetime.utcnow(), channel="system")
        store.append(msg)
        await dispatcher.dispatch(
            citizen_id=citizen_id,
            frame={"type": "agent_message", "convId": conv_id, "agentId": agent_id,
                   "message": msg.model_dump(mode="json")},
            primary_channel=channel)
    except Exception:
        pass
