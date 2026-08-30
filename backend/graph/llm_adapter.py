"""Adapter: our provider config → a LangChain chat model (Layer 2).

- LIVE: Sarvam chat is OpenAI-compatible, so we use `langchain_openai.ChatOpenAI`
  pointed at `https://api.sarvam.ai/v1`. Other OpenAI-compatible providers work
  the same way.
- MOCK: a custom `MockChatModel` that implements *real* tool-calling using our
  existing keyword matcher. This lets the entire LangGraph agent↔tool loop run
  with no API key (demos + tests), exercising the same code path live mode uses.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import json as _json
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (AIMessage, AIMessageChunk, BaseMessage,
                                     ToolMessage)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from ..config import settings
from . import tool_adapter


# ---------------------------------------------------------------------------
# Mock chat model with tool-calling
# ---------------------------------------------------------------------------

class MockChatModel(BaseChatModel):
    """Deterministic, network-free chat model that supports tool-calling.

    Decision logic mirrors the legacy mock path:
      - On a fresh user turn, use the keyword matcher to decide whether to call
        a tool. If yes → emit an AIMessage with `tool_calls`. If no → a short
        canned/persona reply.
      - After a tool has run (a ToolMessage is present), emit a final natural
        confirmation referencing the result.
    """
    agent_id: str = "cmo"
    channel: str = "simulator"
    bound_names: list = []          # sanitised tool names the model may call

    @property
    def _llm_type(self) -> str:
        return "mock-tool-calling"

    def bind_tools(self, tools, **kwargs):
        names = []
        for t in tools or []:
            if isinstance(t, dict):
                names.append(t.get("function", {}).get("name") or t.get("name"))
            else:
                names.append(getattr(t, "name", None))
        return self.model_copy(update={"bound_names": [n for n in names if n]})

    def _latest_human_text(self, messages: list[BaseMessage]) -> str:
        for m in reversed(messages):
            if m.__class__.__name__ == "HumanMessage":
                return m.content if isinstance(m.content, str) else str(m.content)
        return ""

    def _generate(self, messages, stop=None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs) -> ChatResult:
        # If a tool just ran, produce the final answer referencing it.
        last = messages[-1] if messages else None
        if isinstance(last, ToolMessage):
            text = self._reply_after_tool(str(last.content))
            return _wrap(AIMessage(content=text))

        # Fresh user turn → keyword tool decision (same matcher as legacy).
        user_text = self._latest_human_text(messages)
        from ..orchestrator import _mock_pick_tool, _mock_tool_args
        tool = _mock_pick_tool(self.agent_id, user_text)
        if tool:
            clean = tool_adapter.sanitize(tool.id)
            if not self.bound_names or clean in self.bound_names:
                args = _mock_tool_args(tool, self.agent_id, user_text, self.channel)
                return _wrap(AIMessage(content="", tool_calls=[{
                    "name": clean, "args": args,
                    "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call",
                }]))

        # No tool → a short persona/canned reply.
        return _wrap(AIMessage(content=self._canned_reply()))

    def _canned_reply(self) -> str:
        from ..agents import get_agent
        a = get_agent(self.agent_id)
        if a and a.mock_responses:
            # first response is usually the greeting; pick a helpful one
            return a.mock_responses[0]
        return "How can I help you today?"

    def _reply_after_tool(self, tool_content: str) -> str:
        import re
        rid = re.search(r"\b((?:GRV|APP|PRJQ|SRV)-[A-Z]{2}-\d{4}-\d{4,6})\b", tool_content)
        if rid:
            return (f"Done — I've registered this as {rid.group(1)}. "
                    f"You can track it any time with that reference number.")
        if '"ok": true' in tool_content or "'ok': True" in tool_content:
            return "Done — I've taken care of that for you. Anything else?"
        return "Here's what I found. Is there anything else I can help with?"

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, **kwargs)

    # --- streaming (so the graph engine can stream tokens like legacy) ---
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1] if messages else None
        # Tool-result already present → stream the final text reply word by word.
        if isinstance(last, ToolMessage):
            yield from self._stream_text(self._reply_after_tool(str(last.content)), run_manager)
            return
        user_text = self._latest_human_text(messages)
        from ..orchestrator import _mock_pick_tool, _mock_tool_args
        tool = _mock_pick_tool(self.agent_id, user_text)
        if tool:
            clean = tool_adapter.sanitize(tool.id)
            if not self.bound_names or clean in self.bound_names:
                args = _mock_tool_args(tool, self.agent_id, user_text, self.channel)
                # Emit a single tool-call chunk (args as a JSON string fragment).
                chunk = AIMessageChunk(content="", tool_call_chunks=[{
                    "name": clean, "args": _json.dumps(args),
                    "id": f"call_{uuid.uuid4().hex[:8]}", "index": 0,
                }])
                yield ChatGenerationChunk(message=chunk)
                return
        yield from self._stream_text(self._canned_reply(), run_manager)

    def _stream_text(self, text: str, run_manager):
        words = (text or "").split(" ")
        for i, w in enumerate(words):
            piece = w if i == len(words) - 1 else w + " "
            if run_manager:
                run_manager.on_llm_new_token(piece)
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


def _wrap(msg: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=msg)])


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_chat_model(agent_id: str, channel: str = "simulator",
                     provider: Optional[str] = None) -> BaseChatModel:
    """Return a chat model for an agent. Mock when the active LLM is mock.

    `settings.mock_mode` only reflects whether a Sarvam key is present, so it is
    not enough here: demos/tests explicitly set LLM_PROVIDER=mock while a real
    Sarvam key may still exist in .env. Honour that provider switch first.
    """
    if settings.llm_provider == "mock":
        return MockChatModel(agent_id=agent_id, channel=channel)
    try:
        from ..llm import get_llm_for
        if get_llm_for(provider).mock_mode:
            return MockChatModel(agent_id=agent_id, channel=channel)
    except Exception:
        return MockChatModel(agent_id=agent_id, channel=channel)
    # LIVE — Sarvam (OpenAI-compatible). Other providers can be added here.
    try:
        from langchain_openai import ChatOpenAI
        model_id = settings.sarvam_chat_model
        if provider and provider.startswith("sarvam-"):
            model_id = provider  # per-agent pin (e.g. sarvam-105b)
        base = settings.sarvam_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return ChatOpenAI(
            model=model_id, api_key=settings.sarvam_api_key, base_url=base,
            temperature=0.3, timeout=30, max_retries=1, streaming=True,
        )
    except Exception:
        # If langchain-openai isn't available, degrade to mock rather than crash.
        return MockChatModel(agent_id=agent_id, channel=channel)
