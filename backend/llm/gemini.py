"""Google Gemini chat provider.

Gemini's API uses a different shape than OpenAI:
  - Messages live in `contents`, with `role: "user" | "model"` (not "assistant").
  - System instruction goes in `systemInstruction` at the top level.
  - Stream events come from the `:streamGenerateContent?alt=sse` endpoint.

Base: https://generativelanguage.googleapis.com/v1beta
Auth: ?key=<API_KEY> query param OR `x-goog-api-key` header
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

from ..config import settings
from .base import LLMProvider, ProviderInfo, extract_system_and_messages

log = logging.getLogger("llm.gemini")


class GeminiProvider(LLMProvider):
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._api_key = settings.gemini_api_key
        self._model = settings.gemini_model or "gemini-2.0-flash-exp"

    @property
    def mock_mode(self) -> bool:
        return not self._api_key

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="gemini",
            display_name=f"Gemini {self._model}",
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
                    "x-goog-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, read=120.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _adapt_messages(self, messages: list[dict]) -> tuple[Optional[dict], list[dict]]:
        """OpenAI-style -> Gemini-style."""
        system, rest = extract_system_and_messages(messages)
        contents: list[dict] = []
        for m in rest:
            role = m.get("role")
            if role == "system":
                continue
            gem_role = "model" if role == "assistant" else "user"
            contents.append({
                "role": gem_role,
                "parts": [{"text": str(m.get("content", ""))}],
            })
        sys_instr = {"parts": [{"text": system}]} if system else None
        return sys_instr, contents

    async def chat_stream(
        self, *, messages, model=None, temperature=0.4, max_tokens=800,
    ) -> AsyncIterator[str]:
        if self.mock_mode:
            raise RuntimeError("Gemini in mock mode")
        client = await self._client_ref()
        sys_instr, contents = self._adapt_messages(messages)
        mdl = model or self._model
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if sys_instr:
            payload["systemInstruction"] = sys_instr

        url = f"/models/{mdl}:streamGenerateContent?alt=sse"
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                cands = obj.get("candidates", [])
                for c in cands:
                    parts = c.get("content", {}).get("parts", [])
                    for p in parts:
                        t = p.get("text")
                        if t:
                            yield t

    async def chat_complete(
        self, *, messages, model=None, temperature=0.2, max_tokens=800,
        json_mode=False,
    ) -> str:
        if self.mock_mode:
            raise RuntimeError("Gemini in mock mode")
        client = await self._client_ref()
        sys_instr, contents = self._adapt_messages(messages)
        mdl = model or self._model
        gen_cfg: dict = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        payload: dict = {"contents": contents, "generationConfig": gen_cfg}
        if sys_instr:
            payload["systemInstruction"] = sys_instr

        url = f"/models/{mdl}:generateContent"
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        obj = resp.json()
        cands = obj.get("candidates", [])
        if not cands:
            return ""
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
