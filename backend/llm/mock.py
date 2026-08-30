"""MockProvider — emits canned agent responses with realistic streaming.

Used in two scenarios:
  1. LLM_PROVIDER=mock (explicit)
  2. Any other provider with missing credentials falls back to this

In both cases the canned responses are sourced from the agent's
`mock_responses` list (in backend/agents.py) — they're per-agent
plausible answers, so the simulator UI still feels real.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import AsyncIterator, Optional

from .base import LLMProvider, ProviderInfo


class MockProvider(LLMProvider):
    """Always-available provider. Picks an agent-relevant response from the
    in-code mock pool and streams it word-by-word with light latency.

    Pass `provider_tag` to label the mock as "sarvam mock", "openai mock",
    etc. — useful for the status pill so users see *why* they're in mock
    mode.
    """

    def __init__(self, provider_tag: str = "mock", agent_mock_proxy=None) -> None:
        self._tag = provider_tag
        # Optional reference to the "real" provider that fell back to mock.
        # Today unused, but a future enhancement could call agent_mock_proxy.info()
        # to show what model the user was *trying* to use.
        self._proxy = agent_mock_proxy

    @property
    def mock_mode(self) -> bool:
        return True

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=f"mock({self._tag})",
            display_name=f"MOCK fallback ({self._tag})",
            is_sovereign=(self._tag == "sarvam"),
            is_mock=True,
            model_id="mock",
            base_url="",
        )

    # -----------------------------------------------------------------
    # Streaming chat
    # -----------------------------------------------------------------
    async def chat_stream(self, *, messages, model=None, temperature=0.4,
                          max_tokens=800) -> AsyncIterator[str]:
        text = self._pick_response(messages)
        words = re.findall(r"\S+\s*", text)
        for w in words:
            await asyncio.sleep(0.04 + random.random() * 0.04)
            yield w

    async def chat_complete(self, *, messages, model=None, temperature=0.2,
                            max_tokens=800, json_mode=False) -> str:
        if json_mode:
            from ..language import resolve_turn_language
            return self._mock_json_route(messages)
        return self._pick_response(messages)

    # -----------------------------------------------------------------
    # Pickers (lifted from the old SarvamClient mock path)
    # -----------------------------------------------------------------
    def _pick_response(self, messages: list[dict]) -> str:
        # Lazy import to avoid a circular dependency between llm/ and agents
        from ..agents import all_agents
        from ..language import choose_response_for_language, resolve_turn_language

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_text = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        target_lang = "en-IN"
        m = re.search(r"detected language:\s*([a-z]{2,3}-IN)", system, re.I)
        if m:
            target_lang = m.group(1)
        target_lang = resolve_turn_language(
            user_text, current_lang=target_lang, state_default=target_lang,
        )

        for agent in all_agents():
            if agent.name in system:
                return choose_response_for_language(
                    list(agent.mock_responses),
                    target_lang=target_lang,
                    user_text=user_text,
                )
        return "Welcome! How may I help you today?"

    def _mock_json_route(self, messages: list[dict]) -> str:
        from ..agents import AGENTS
        from ..language import resolve_turn_language
        user_text = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        t = user_text.lower()
        rules = [
            (r"\b(patta|chitta|adangal|land|encumbrance|ec|registr)", "revenue"),
            (r"\b(licence|license|dl|rto|bus|vehicle|fitness|permit)", "transport"),
            (r"\b(ration|rice|sugar|pds|onorc)", "ration"),
            (r"\b(water|leak|tanker|supply|sewer|metrowater)", "water"),
            (r"\b(ambulance|hospital|vaccin|108|fever|sick|dengue|covid|health)", "health"),
            (r"\b(crop|soil|kisan|kcc|pmkisan|fertili|msp|paddy|wheat)", "agriculture"),
            (r"\b(grievance|complain|scheme|magalir|cm|relief)", "cmo"),
        ]
        primary = "cmo"
        for pat, agent in rules:
            if re.search(pat, t):
                primary = agent
                break
        return json.dumps({
            "primaryAgent": primary,
            "secondaryAgents": [],
            "intent": "general_query",
            "confidence": 0.6,
            "language": resolve_turn_language(
                user_text, current_lang="en-IN", state_default="en-IN"),
            "requiresHandoff": False,
        })
