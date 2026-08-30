"""Example drop-in tool plugin — property-tax dues lookup.

This file demonstrates the developer workflow: write an async function,
annotate it with `@tool`, save it here. On the next startup (or Tools-page
"Reload") it appears in the registry and on the admin Tools page, where an
operator can enable it and wire it to agents — no edits to tools.py.

The implementation below is a deterministic mock so the example is safe to
ship; swap the body for a real revenue-system call when one is available.
"""
from __future__ import annotations

from backend.tool_sdk import tool


@tool(
    id="revenue.check_dues",
    name="Check property tax dues",
    category="revenue",
    description=("Look up outstanding property tax for a property. The SURVEY "
                 "NUMBER alone is sufficient — call this tool immediately with "
                 "just survey_no. Do NOT ask the citizen for their city, "
                 "district or municipality; this lookup does not need them. "
                 "Use whenever a citizen asks how much property/house tax they owe."),
    input_schema={
        "type": "object",
        "properties": {
            "survey_no": {"type": "string",
                          "description": "Survey number of the property — the only input needed."},
        },
        "required": ["survey_no"],
    },
    requires_consent=False,
    default_agents=["revenue", "cmo"],   # suggested wiring; operator can override
    # Keyword triggers so the legacy keyword-matching orchestrator can pick this
    # tool from chat (Sarvam function-calling isn't wired). Without these the
    # tool is only reachable via the Tools-page Test button.
    keywords=[r"property tax", r"house tax", r"\bproperty\b.{0,15}\btax\b",
              r"tax (due|owe|owed|outstanding|dues|pending)",
              r"\bdues?\b.{0,15}\btax\b", r"संपत्ति कर", r"गृह कर",
              r"சொத்து வரி", r"வீட்டு வரி"],
)
async def check_dues(args: dict, citizen_id: str) -> dict:
    survey = (args.get("survey_no") or "").strip()
    if not survey:
        return {"ok": False, "error": "survey_no_required",
                "message": "Ask the citizen for their property survey number."}
    # Deterministic mock: derive a plausible figure from the survey number so
    # the same input always returns the same dues (good for demos/tests).
    digits = "".join(ch for ch in survey if ch.isdigit()) or "0"
    dues = (int(digits[-4:]) % 9000) + 250
    return {
        "ok": True,
        "survey_no": survey,
        "dues_rs": dues,
        "assessment_year": "2025-26",
        "message": (f"Outstanding property tax for survey {survey} is "
                    f"₹{dues}. Pay before 31 Mar to avoid penalty."),
        "is_mock": True,
    }
