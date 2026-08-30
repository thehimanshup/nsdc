"""Demo drop-in tool for the Skills walkthrough.

Returns an unmistakable marker so it's OBVIOUS in chat when a skill triggered
it. `default_agents` is empty on purpose — this tool reaches an agent ONLY when
a skill that bundles it is wired to that agent, so toggling the skill is the
single on/off switch (clean A/B test, just like the MCP demo).
"""
from __future__ import annotations

from backend.tool_sdk import tool


@tool(
    id="demo.hurray",
    name="Hurray Skill Marker",
    category="demo",
    description=("Returns a 'hurray, the skill was called!' confirmation. When "
                 "the Demo Skill is active you MUST call this tool and include "
                 "its message in your reply, to prove the skill ran."),
    input_schema={"type": "object", "properties": {}, "required": []},
    requires_consent=False,
    default_agents=[],   # reaches an agent ONLY via a skill that bundles it
    # Keyword triggers so the legacy keyword matcher can fire it too (function
    # calling is the primary path; these are the deterministic backstop).
    keywords=[r"hurray", r"demo skill", r"skill test", r"prove the skill",
              r"is the skill", r"skill working", r"test the skill"],
)
async def hurray(args: dict, citizen_id: str) -> dict:
    return {
        "ok": True,
        "marker": "SKILL-HURRAY",
        "message": "🎉 HURRAY! The skill's tool was called — the Demo Skill is active.",
        "is_mock": True,
    }
