"""Agent Templates — Phase 6e.

Generic archetypes an admin instantiates to mint a new department agent
without hand-writing JSON. Instantiating writes a new entry to agents.json
via admin_storage.save_agent (hot-reloaded), and is how the four Phase-6e
agents (housing, wcd, social, pwd) are intended to be created.
"""
from __future__ import annotations

import logging
from typing import Optional

from .agents import Agent
from . import admin_storage

log = logging.getLogger("agent_templates")

# Common tool bundles
_RECORD_TOOLS = ["records.create", "records.track", "records.list_mine",
                 "records.send_reminder", "records.submit_feedback"]
_SCHEME_TOOLS = ["schemes.search", "schemes.check_eligibility", "schemes.apply"]
_PROJECT_TOOLS = ["projects.find_near_me", "projects.track", "projects.report_issue"]


TEMPLATES: dict[str, dict] = {
    "welfare_scheme_desk": {
        "id": "welfare_scheme_desk",
        "label": "Welfare scheme desk",
        "description": "Empathetic desk that finds schemes, checks eligibility, helps apply and tracks applications.",
        "tone": "empathetic",
        "tool_ids": _SCHEME_TOOLS + _RECORD_TOOLS,
        "traits": ["explains eligibility rule by rule in plain language",
                   "offers to check eligibility before asking to apply",
                   "always gives the reference number and how to track it"],
        "default_emoji": "🤝", "default_color": "#00695C", "default_bg": "#e0f2f1",
    },
    "grievance_desk": {
        "id": "grievance_desk",
        "label": "Grievance desk",
        "description": "Registers trackable complaints with L1-L4 escalation and SLA timers; supports reminders + feedback.",
        "tone": "warm-helpful",
        "tool_ids": _RECORD_TOOLS,
        "traits": ["validates the citizen's frustration before giving the procedure",
                   "gives a reference number, the owning desk and the SLA promise",
                   "explains how escalation works if it isn't resolved in time"],
        "default_emoji": "📨", "default_color": "#E65100", "default_bg": "#fff3e0",
    },
    "service_request_desk": {
        "id": "service_request_desk",
        "label": "Service request desk",
        "description": "Handles operational requests (connections, renewals) with optional DigiLocker document fetch.",
        "tone": "brisk",
        "tool_ids": _RECORD_TOOLS,
        "traits": ["operationally focused — short, factual replies",
                   "gives complaint IDs, ETAs, helpline numbers",
                   "asks for the minimum info needed, then files the request"],
        "default_emoji": "🧾", "default_color": "#1565C0", "default_bg": "#e3f2fd",
    },
    "project_info_desk": {
        "id": "project_info_desk",
        "label": "Project info desk",
        "description": "Read-mostly desk to find and track development projects and report issues.",
        "tone": "matter-of-fact",
        "tool_ids": _PROJECT_TOOLS + ["records.track", "records.list_mine"],
        "traits": ["gives project ids, percent complete and ETAs",
                   "turns an issue report into a trackable grievance",
                   "names the contractor and sanctioned cost when asked"],
        "default_emoji": "🚧", "default_color": "#455A64", "default_bg": "#eceff1",
    },
}


def list_templates() -> list[dict]:
    return list(TEMPLATES.values())


def instantiate(*, template_id: str, agent_id: str, name: str,
                emoji: str = "", color: str = "", bg: str = "",
                persona_name: str = "", state_scope: str = "",
                department_block: str = "", enabled: bool = True) -> Optional[Agent]:
    tpl = TEMPLATES.get(template_id)
    if not tpl:
        return None
    agent = Agent(
        id=agent_id, name=name,
        emoji=emoji or tpl["default_emoji"],
        color=color or tpl["default_color"],
        bg=bg or tpl["default_bg"],
        description=tpl["description"],
        department_block=department_block or
            f"You are the {name} helpdesk. {tpl['description']} "
            f"Use your tools to take real, trackable action for the citizen.",
        mock_responses=[f"{tpl['default_emoji']} Welcome to {name}. How can I help?"],
        push_pool=[f"{tpl['default_emoji']} Update from {name}"],
        voice="shubh", enabled=enabled,
        tool_ids=list(tpl["tool_ids"]),
        persona_name=persona_name or f"{name} officer",
        tone=tpl["tone"],
        signature_opener=f"Vanakkam! Welcome to {name}. I'm here to help.",
        signature_closer="Anything else I can help with?",
        conversational_traits=list(tpl["traits"]),
    )
    admin_storage.save_agent(agent)
    log.info("Instantiated agent '%s' from template '%s'", agent_id, template_id)
    return agent
