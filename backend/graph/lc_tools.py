"""Keystone adapter: our `tools.py` registry -> first-class LangChain tools.

Phase 7 (Milestone 1). Instead of handing raw OpenAI function-schema dicts to
`model.bind_tools(...)` and executing through a bespoke loop, we wrap each
registered `Tool` as a LangChain `StructuredTool`. The model then binds real
LangChain tools, and a single execution path (the StructuredTool's coroutine)
is reused by both `node_agent` (binding) and `node_tools` (execution) — and,
later, by a LangGraph `ToolNode`.

Execution still routes through `orchestrator._execute_tool_and_append`, so
consent, audit, PII redaction, persistence and dispatch all run unchanged.

Runtime context (citizen/conv/channel/agent) isn't part of the model-facing
tool args, so the graph sets it per turn via `set_turn_context()` and the
coroutine reads it from a ContextVar. This keeps the LangChain tool signature
clean (only the schema-declared fields) while preserving the existing call
contract.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from langchain_core.tools import StructuredTool

from ..config import settings
from ..tools import Tool, tools_for_agent, get_tool
from .tool_adapter import sanitize


@dataclass
class _TurnContext:
    citizen_id: str
    conv_id: str
    channel: str
    agent_id: str


# Per-turn execution context, set by the graph before tools may run.
_turn_ctx: ContextVar[Optional[_TurnContext]] = ContextVar("lc_turn_ctx", default=None)


def set_turn_context(citizen_id: str, conv_id: str, channel: str, agent_id: str) -> None:
    """Bind the runtime context the tool coroutines need. Called by the graph
    nodes before binding/executing tools for a turn."""
    _turn_ctx.set(_TurnContext(citizen_id=citizen_id, conv_id=conv_id,
                               channel=channel, agent_id=agent_id))


def _make_coroutine(t: Tool):
    async def _run(**kwargs) -> dict:
        from ..orchestrator import _execute_tool_and_append
        ctx = _turn_ctx.get()
        if ctx is None:
            # Defensive: a tool should never execute before the graph sets the
            # turn context. Fail with a structured result rather than crash.
            return {"ok": False, "error": "no_turn_context"}
        # Re-attach the implicit args the legacy executor expects but that aren't
        # part of the model-facing schema.
        args = {**kwargs, "_channel": ctx.channel, "agent_id": ctx.agent_id}
        return await _execute_tool_and_append(
            ctx.citizen_id, ctx.conv_id, t, args=args, channel=ctx.channel,
            return_result=True)
    return _run


def to_langchain_tool(t: Tool) -> StructuredTool:
    """Wrap a registry `Tool` as a LangChain `StructuredTool`.

    `args_schema` takes the JSON-schema dict directly (langchain-core 1.x
    accepts `dict[str, Any]`). The sanitised id is the tool name (OpenAI tool
    names disallow dots). The coroutine returns the raw result dict; the graph's
    tools node serialises it into the ToolMessage."""
    return StructuredTool(
        name=sanitize(t.id),
        description=t.description,
        args_schema=t.input_schema or {"type": "object", "properties": {}},
        coroutine=_make_coroutine(t),
    )


def _tools_for_agent(agent_id: str) -> list[Tool]:
    """The agent's directly-wired tools plus any brought by attached skills.

    Skill tools are added by id (deduped against the direct set) but still
    respect a global disable in tool_bindings — a skill grants an agent access
    to a tool, it doesn't re-enable a tool the operator switched off."""
    seen: dict[str, Tool] = {t.id: t for t in tools_for_agent(agent_id)}
    if settings.skills_enabled:
        from ..skills import skills_for_agent
        from .. import tool_bindings
        for sk in skills_for_agent(agent_id):
            for tid in sk.tool_ids:
                if tid in seen:
                    continue
                b = tool_bindings.get(tid)
                if b is not None and not b.get("enabled", True):
                    continue
                t = get_tool(tid)
                if t is not None:
                    seen[tid] = t
    return list(seen.values())


def langchain_tools_for_agent(agent_id: str) -> list[StructuredTool]:
    """LangChain tools an agent may use this turn (direct wiring + skills)."""
    return [to_langchain_tool(t) for t in _tools_for_agent(agent_id)]
