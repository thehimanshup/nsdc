"""Tool SDK — write a tool the way you'd write an MCP tool, drop it in.

A developer defines a plain async function, annotates it with `@tool(...)`,
and saves the file under `backend/tool_plugins/`. The discovery loader
(`tool_loader.py`) imports that folder at startup, which fires every
`@tool` decorator. The decorator just builds a normal `Tool` and calls the
existing `register()` — so nothing downstream changes: consent, audit, PII
redaction, the dedupe guard and `tools_for_agent()` all keep working exactly
as they do for the built-in tools.

Example — backend/tool_plugins/check_dues.py::

    from backend.tool_sdk import tool

    @tool(
        id="revenue.check_dues",
        name="Check property tax dues",
        category="revenue",
        description="Look up outstanding property tax for a survey number.",
        input_schema={"type": "object",
                      "properties": {"survey_no": {"type": "string"}},
                      "required": ["survey_no"]},
        requires_consent=False,
        default_agents=["revenue", "cmo"],   # suggested wiring; operator can override
    )
    async def check_dues(args, citizen_id):
        return {"ok": True, "dues_rs": 1240, "survey_no": args["survey_no"]}

`default_agents` is only a *suggestion*: the live wiring lives in
`data/tool_bindings.json` (edited from the Tools page). If a tool has no
binding yet, `tools_for_agent()` falls back to this list.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from .tools import Tool, register

ExecuteFn = Callable[[dict, str], Awaitable[dict]]


def tool(
    *,
    id: str,
    name: str,
    description: str,
    input_schema: Optional[dict] = None,
    category: str = "",
    connector: str = "",
    requires_consent: bool = False,
    consent_scope: str = "",
    default_agents: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    sla_p95_ms: int = 1500,
    source: str = "plugin",
) -> Callable[[ExecuteFn], ExecuteFn]:
    """Decorator that registers an async function as a Tool.

    The decorated function must have signature `async def fn(args, citizen_id)`
    and return a JSON-serialisable dict. The original function is returned
    unchanged so it stays directly callable/testable.
    """

    def _decorate(fn: ExecuteFn) -> ExecuteFn:
        register(Tool(
            id=id,
            name=name,
            description=description,
            connector=connector or (category or "plugin"),
            requires_consent=requires_consent,
            consent_scope=consent_scope,
            input_schema=input_schema or {"type": "object", "properties": {},
                                          "required": []},
            allowed_agents=list(default_agents or []),
            execute=fn,
            sla_p95_ms=sla_p95_ms,
            category=category,
            source=source,
            trigger_patterns=list(keywords or []),
        ))
        return fn

    return _decorate
