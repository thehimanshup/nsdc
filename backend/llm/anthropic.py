"""Anthropic Claude chat provider.

Anthropic's API differs from OpenAI's in two material ways:
  1. System messages go in a top-level `system` field, not inside `messages`.
  2. Streaming SSE uses event-typed frames (content_block_delta, etc.)
     instead of OpenAI's simple delta-content payload.

Base: https://api.anthropic.com/v1
Auth: x-api-key: <KEY> + anthropic-version: 2023-06-01
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

from ..config import settings
from .base import LLMProvider, ProviderInfo, extract_system_and_messages

log = logging.getLogger("llm.anthropic")


class AnthropicProvider(LLMProvider):
    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._api_key = settings.anthropic_api_key
        self._model = settings.anthropic_model or "claude-3-5-sonnet-20241022"

    @property
    def mock_mode(self) -> bool:
        return not self._api_key

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="anthropic",
            display_name=f"Anthropic {self._model}",
            is_sovereign=False,
            is_mock=self.mock_mode,
            model_id=self._model,
            base_url=self.BASE_URL,
        )

    async def _client_ref(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, read=120.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _adapt_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Strip system message; ensure assistant/user alternation Anthropic likes."""
        system, rest = extract_system_and_messages(messages)
        # Anthropic disallows trailing assistant messages and consecutive
        # same-role messages. Collapse consecutive same-role messages.
        collapsed: list[dict] = []
        for m in rest:
            role = m.get("role", "user")
            if role == "system":
                continue
            content = m.get("content", "")
            if collapsed and collapsed[-1]["role"] == role:
                collapsed[-1]["content"] += "\n\n" + str(content)
            else:
                collapsed.append({"role": role, "content": str(content)})
        return system, collapsed

    async def chat_stream(
        self, *, messages, model=None, temperature=0.4, max_tokens=800,
    ) -> AsyncIterator[str]:
        if self.mock_mode:
            raise RuntimeError("Anthropic in mock mode")
        client = await self._client_ref()
        system, msgs = self._adapt_messages(messages)
        payload = {
            "model": model or self._model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system

        async with client.stream("POST", "/messages", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event_type = obj.get("type")
                if event_type == "content_block_delta":
                    delta = obj.get("delta", {}).get("text")
                    if delta:
                        yield delta
                elif event_type == "message_stop":
                    break

    async def chat_complete(
        self, *, messages, model=None, temperature=0.2, max_tokens=800,
        json_mode=False,
    ) -> str:
        if self.mock_mode:
            raise RuntimeError("Anthropic in mock mode")
        client = await self._client_ref()
        system, msgs = self._adapt_messages(messages)
        # Anthropic doesn't have OpenAI's response_format; for JSON mode we
        # encourage the model via system prompt.
        if json_mode and system:
            system = system + "\n\nRespond with ONLY valid JSON. No prose."
        elif json_mode:
            system = "Respond with ONLY valid JSON. No prose."

        payload: dict = {
            "model": model or self._model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system

        resp = await client.post("/messages", json=payload)
        resp.raise_for_status()
        obj = resp.json()
        # Response shape: {"content": [{"type":"text", "text":"..."}]}
        for block in obj.get("content", []):
            if block.get("type") == "text":
                return block.get("text", "")
        return ""
