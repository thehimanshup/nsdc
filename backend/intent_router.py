"""Sarvam-30B JSON-mode intent classifier.

Picks the right department agent for a citizen's query. Falls back to the
mock router when SARVAM_API_KEY is absent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .agents import AGENTS
from .llm import llm
from .sarvam_client import detect_language_naive

ROUTER_SYSTEM = """You classify Indian citizen queries into one of seven government departments.

Available agents:
- agriculture  — crop subsidies, KCC, PM-KISAN, soil health, MSP
- water        — water supply, leaks, bills, sewerage, tankers
- cmo          — Chief Minister's Office, grievances, welfare schemes, escalation
- health       — hospitals, ambulance (108), vaccines, blood, mental health
- revenue      — Patta, Chitta, EC, land records, death cert, relief applications
- transport    — driving licence, bus pass, fitness, MACT, bus tracking
- ration       — ration card, monthly allocation, Aadhaar seeding, ONORC

Return ONLY valid JSON. No prose, no markdown. Shape:
{
  "primaryAgent": "<one of the 7 ids above>",
  "secondaryAgents": [],
  "intent": "<short snake_case label>",
  "confidence": <0.0 to 1.0>,
  "language": "<ISO code like ta-IN, hi-IN, en-IN>",
  "requiresHandoff": false
}

If the query mentions multiple departments (e.g. flood damaged a farm — touches Revenue,
Agriculture, and CMO), put the most relevant in primaryAgent and others in secondaryAgents.
"""


@dataclass
class Route:
    primary_agent: str
    secondary_agents: list[str]
    intent: str
    confidence: float
    language: str
    requires_handoff: bool

    @classmethod
    def fallback(cls, text: str, *, default: str = "cmo") -> "Route":
        return cls(
            primary_agent=default,
            secondary_agents=[],
            intent="unclassified",
            confidence=0.3,
            language=detect_language_naive(text),
            requires_handoff=False,
        )


async def classify(
    *,
    text: str,
    active_agent: Optional[str] = None,
    history: Optional[list[dict]] = None,
) -> Route:
    """Classify the citizen's query to a target agent.

    Optimisation: if active_agent is already set and the message is short
    (likely a follow-up), skip the LLM call.
    """
    # Cheap fast-path: continuing conversation, no router call needed
    if active_agent and active_agent in AGENTS and len(text.split()) <= 4:
        return Route(
            primary_agent=active_agent,
            secondary_agents=[],
            intent="continuation",
            confidence=0.9,
            language=detect_language_naive(text),
            requires_handoff=False,
        )

    msgs = [{"role": "system", "content": ROUTER_SYSTEM}]
    if history:
        # Use a compact recent history (last 3 exchanges) for context
        msgs.extend(history[-6:])
    msgs.append({"role": "user", "content": text})

    try:
        raw = await llm.chat_complete(messages=msgs, json_mode=True, temperature=0.1)
        obj = json.loads(raw)
        primary = obj.get("primaryAgent", "cmo")
        if primary not in AGENTS:
            primary = "cmo"
        secondaries = [a for a in obj.get("secondaryAgents", []) if a in AGENTS]
        return Route(
            primary_agent=primary,
            secondary_agents=secondaries,
            intent=obj.get("intent", "general_query"),
            confidence=float(obj.get("confidence", 0.6)),
            language=obj.get("language", detect_language_naive(text)),
            requires_handoff=bool(obj.get("requiresHandoff", False)),
        )
    except Exception:
        return Route.fallback(text, default=active_agent or "cmo")
