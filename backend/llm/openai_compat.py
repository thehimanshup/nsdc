"""OpenAI-compatible chat providers.

Many providers speak the OpenAI Chat Completions API by simply pointing
to a different base URL — this includes:
  - OpenAI itself     (api.openai.com/v1)
  - Groq              (api.groq.com/openai/v1)
  - Together AI       (api.together.xyz/v1)
  - Mistral La Plateforme (api.mistral.ai/v1)
  - Ollama (local)    (localhost:11434/v1)
  - OpenRouter, Fireworks, Perplexity, Cerebras, etc.

So we have one base class plus thin subclasses that override the
config keys.

NOTE: per the v2 architecture's sovereignty mandate, ANY of these
overseas providers ship citizen data out of India. We surface this in
ProviderInfo.is_sovereign — the simulator status pill colours the
non-sovereign providers amber.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

from ..config import settings
from .base import LLMProvider, ProviderInfo

log = logging.getLogger("llm.openai_compat")


class _OpenAICompatBase(LLMProvider):
    """Shared streaming + non-streaming logic for OpenAI-compatible endpoints."""

    # Override these in subclasses
    PROVIDER_NAME = "openai"
    DISPLAY_NAME = "OpenAI"
    DEFAULT_BASE = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"
    IS_SOVEREIGN = False
    AUTH_SCHEME = "Bearer"   # most use "Bearer <key>"; override to "" for Ollama

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._api_key = self._read_api_key()
        self._base_url = self._read_base_url()
        self._model = self._read_model()

    # Subclasses override these to point at the right env vars
    def _read_api_key(self) -> str:
        return settings.openai_api_key

    def _read_base_url(self) -> str:
        return settings.openai_base_url or self.DEFAULT_BASE

    def _read_model(self) -> str:
        return settings.openai_model or self.DEFAULT_MODEL

    @property
    def mock_mode(self) -> bool:
        # Ollama and other local providers don't need a key
        if self.AUTH_SCHEME == "":
            return False
        return not self._api_key

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.PROVIDER_NAME,
            display_name=f"{self.DISPLAY_NAME} {self._model}",
            is_sovereign=self.IS_SOVEREIGN,
            is_mock=self.mock_mode,
            model_id=self._model,
            base_url=self._base_url,
        )

    async def _client_ref(self) -> httpx.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self.AUTH_SCHEME == "Bearer" and self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            elif self._api_key:
                headers["Authorization"] = f"{self.AUTH_SCHEME} {self._api_key}".strip()
            self._client = httpx.AsyncClient(
                base_url=self._base_url, headers=headers,
                timeout=httpx.Timeout(30.0, read=120.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat_stream(
        self, *, messages, model=None, temperature=0.4, max_tokens=800,
    ) -> AsyncIterator[str]:
        if self.mock_mode:
            raise RuntimeError(f"{self.PROVIDER_NAME} provider in mock mode")
        client = await self._client_ref()
        payload = {
            "model": model or self._model,
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
                delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta

    async def chat_complete(
        self, *, messages, model=None, temperature=0.2, max_tokens=800,
        json_mode=False,
    ) -> str:
        if self.mock_mode:
            raise RuntimeError(f"{self.PROVIDER_NAME} provider in mock mode")
        client = await self._client_ref()
        payload: dict = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        obj = resp.json()
        return obj["choices"][0]["message"].get("content") or ""


# ---------------------------------------------------------------------------
# Concrete OpenAI-compatible providers
# ---------------------------------------------------------------------------

class OpenAIProvider(_OpenAICompatBase):
    PROVIDER_NAME = "openai"
    DISPLAY_NAME = "OpenAI"
    DEFAULT_BASE = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"


class GroqProvider(_OpenAICompatBase):
    PROVIDER_NAME = "groq"
    DISPLAY_NAME = "Groq"
    DEFAULT_BASE = "https://api.groq.com/openai/v1"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"

    def _read_api_key(self) -> str:
        return settings.groq_api_key

    def _read_base_url(self) -> str:
        return settings.groq_base_url or self.DEFAULT_BASE

    def _read_model(self) -> str:
        return settings.groq_model or self.DEFAULT_MODEL


class TogetherProvider(_OpenAICompatBase):
    PROVIDER_NAME = "together"
    DISPLAY_NAME = "Together AI"
    DEFAULT_BASE = "https://api.together.xyz/v1"
    DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

    def _read_api_key(self) -> str:
        return settings.together_api_key

    def _read_base_url(self) -> str:
        return settings.together_base_url or self.DEFAULT_BASE

    def _read_model(self) -> str:
        return settings.together_model or self.DEFAULT_MODEL


class MistralProvider(_OpenAICompatBase):
    PROVIDER_NAME = "mistral"
    DISPLAY_NAME = "Mistral"
    DEFAULT_BASE = "https://api.mistral.ai/v1"
    DEFAULT_MODEL = "mistral-large-latest"

    def _read_api_key(self) -> str:
        return settings.mistral_api_key

    def _read_base_url(self) -> str:
        return settings.mistral_base_url or self.DEFAULT_BASE

    def _read_model(self) -> str:
        return settings.mistral_model or self.DEFAULT_MODEL


class OllamaProvider(_OpenAICompatBase):
    """Local Ollama server. No auth needed by default.
    Use for offline / on-prem deployments — fully sovereign even though
    Ollama itself runs the model, because everything stays on your hardware.
    """
    PROVIDER_NAME = "ollama"
    DISPLAY_NAME = "Ollama (local)"
    DEFAULT_BASE = "http://localhost:11434/v1"
    DEFAULT_MODEL = "llama3.2"
    AUTH_SCHEME = ""    # no auth
    IS_SOVEREIGN = True  # local = sovereign by construction

    def _read_api_key(self) -> str:
        return settings.ollama_api_key   # usually empty

    def _read_base_url(self) -> str:
        return settings.ollama_base_url or self.DEFAULT_BASE

    def _read_model(self) -> str:
        return settings.ollama_model or self.DEFAULT_MODEL

    @property
    def mock_mode(self) -> bool:
        # Ollama never needs a key; if base URL is set, assume it's reachable
        return False
