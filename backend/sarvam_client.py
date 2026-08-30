"""Sarvam AI client wrapper.

Calls the real Sarvam API when SARVAM_API_KEY is set, falls back to a smart
mock when it isn't. This lets you exercise the full UI/orchestration path
without an API key — useful for the first day of integration.

Auth header: 'api-subscription-key' (NOT 'Authorization: Bearer').
Docs: https://docs.sarvam.ai/
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import AsyncIterator, Optional

import httpx

from .config import settings


# ---------------------------------------------------------------------------
# Mock-mode helpers
# ---------------------------------------------------------------------------

# Very lightweight language guesser used only in mock mode. The real path
# uses Sarvam-30B with a structured-output prompt.
_SCRIPT_RANGES = {
    "ta-IN": (0x0B80, 0x0BFF),    # Tamil
    "hi-IN": (0x0900, 0x097F),    # Devanagari (Hindi, Marathi, etc.)
    "bn-IN": (0x0980, 0x09FF),    # Bengali
    "te-IN": (0x0C00, 0x0C7F),    # Telugu
    "kn-IN": (0x0C80, 0x0CFF),    # Kannada
    "ml-IN": (0x0D00, 0x0D7F),    # Malayalam
    "gu-IN": (0x0A80, 0x0AFF),    # Gujarati
    "pa-IN": (0x0A00, 0x0A7F),    # Gurmukhi (Punjabi)
    "or-IN": (0x0B00, 0x0B7F),    # Odia (note: Sarvam uses od-IN — we map)
}


def detect_language_naive(text: str) -> str:
    """Fast script-based language guess — used in mock mode only."""
    if not text:
        return "en-IN"
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for lang, (lo, hi) in _SCRIPT_RANGES.items():
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts:
        return "en-IN"
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    # Sarvam's Odia code is od-IN not or-IN
    return "od-IN" if best == "or-IN" else best


# ---------------------------------------------------------------------------
# Real Sarvam client
# ---------------------------------------------------------------------------

class SarvamClient:
    """Async wrapper over Sarvam's REST API using httpx.

    Only the endpoints used in Phase 1 are implemented:
      - chat completions (streaming)
      - simple translate (used by the simulator's language toggle later)
    Phase 2 adds Saaras (STT) and Bulbul (TTS).
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.sarvam_base_url,
                headers={
                    "api-subscription-key": settings.sarvam_api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, read=60.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -----------------------------------------------------------------
    # Chat completions
    # -----------------------------------------------------------------
    async def chat_stream(
        self,
        *,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 800,           # ≥500 to avoid content=None gotcha
    ) -> AsyncIterator[str]:
        """Yield text chunks as they stream from Sarvam.

        In mock mode, yields a canned response one chunk at a time so the
        simulator's typing animation still feels right.
        """
        if settings.mock_mode:
            if not settings.allow_mock_providers:
                raise RuntimeError("Sarvam chat unavailable: SARVAM_API_KEY is required and mock fallback is disabled")
            async for chunk in self._mock_chat_stream(messages):
                yield chunk
            return

        client = await self._ensure_client()
        payload = {
            "model": model or settings.sarvam_chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (
                    obj.get("choices", [{}])[0].get("delta", {}).get("content")
                    or obj.get("choices", [{}])[0].get("delta", {}).get("reasoning_content")
                )
                if delta:
                    yield delta

    async def chat_complete(
        self,
        *,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        json_mode: bool = False,
    ) -> str:
        """Non-streaming completion. Used for intent routing (JSON output)."""
        if settings.mock_mode:
            if not settings.allow_mock_providers:
                raise RuntimeError("Sarvam chat unavailable: SARVAM_API_KEY is required and mock fallback is disabled")
            return await self._mock_chat_complete(messages, json_mode=json_mode)

        client = await self._ensure_client()
        payload: dict = {
            "model": model or settings.sarvam_chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        obj = resp.json()
        choice = obj["choices"][0]["message"]
        # Gotcha: content can be None if reasoning ate the token budget
        return choice.get("content") or choice.get("reasoning_content") or ""

    # -----------------------------------------------------------------
    # Mock-mode chat (used when no SARVAM_API_KEY)
    # -----------------------------------------------------------------
    async def _mock_chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Pick a plausible canned response and stream it word-by-word."""
        text = self._mock_pick_response(messages)
        # Stream word-by-word with small delays — feels like a real LLM
        words = re.findall(r"\S+\s*", text)
        for w in words:
            await asyncio.sleep(0.04 + random.random() * 0.04)
            yield w

    async def _mock_chat_complete(self, messages: list[dict], *, json_mode: bool) -> str:
        if json_mode:
            from .language import resolve_turn_language
            # Pretend to be the intent router
            user_text = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
            )
            primary = self._mock_route(user_text)
            return json.dumps({
                "primaryAgent": primary,
                "secondaryAgents": [],
                "intent": "general_query",
                "confidence": 0.6,
                "language": resolve_turn_language(user_text, current_lang="en-IN",
                                                  state_default="en-IN"),
                "requiresHandoff": False,
            })
        return self._mock_pick_response(messages)

    def _mock_pick_response(self, messages: list[dict]) -> str:
        """Mock-mode response picker — uses agent context from system prompt."""
        from .agents import AGENTS, all_agents
        from .language import choose_response_for_language, resolve_turn_language

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

        # Find which agent's prompt we have
        for agent in all_agents():
            if agent.name in system:
                return choose_response_for_language(
                    list(agent.mock_responses),
                    target_lang=target_lang,
                    user_text=user_text,
                )

        return "Welcome! How may I help you today?"

    def _mock_route(self, text: str) -> str:
        """Naive keyword routing for mock-mode intent classification."""
        t = text.lower()
        rules = [
            (r"\b(patta|chitta|adangal|land|encumbrance|ec|registr)", "revenue"),
            (r"\b(licence|license|dl|rto|bus|vehicle|fitness|permit)", "transport"),
            (r"\b(ration|rice|sugar|pds|onorc)", "ration"),
            (r"\b(water|leak|tanker|supply|sewer|metrowater)", "water"),
            (r"\b(ambulance|hospital|vaccin|108|fever|sick|dengue|covid|health)", "health"),
            (r"\b(crop|soil|kisan|kcc|pmkisan|fertili|msp|paddy|wheat)", "agriculture"),
            (r"\b(grievance|complain|scheme|magalir|cm|relief)", "cmo"),
        ]
        for pat, agent in rules:
            if re.search(pat, t):
                return agent
        return "cmo"  # Default escalation channel


# Module-level singleton
sarvam = SarvamClient()
