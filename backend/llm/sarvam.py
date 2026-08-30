"""Sarvam-30B / 105B chat provider.

Auth: `api-subscription-key` header (NOT `Authorization: Bearer`).
Base: https://api.sarvam.ai/v1

Gotchas (from the Sarvam skills repo):
  - `content` can be `None` if max_tokens is too low â€” Sarvam reasons
    internally before emitting content. Keep max_tokens >= 500.
  - `reasoning_content` may arrive before `content`. We treat
    reasoning_content as a fallback when content is null.
  - `reasoning_effort` controls thinking depth â€” "low" | "medium" | "high".
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

from ..config import settings
from .base import LLMProvider, ProviderInfo

log = logging.getLogger("llm.sarvam")


class SarvamProvider(LLMProvider):
    # Subclasses pin a specific model. Default uses settings.sarvam_chat_model
    # (which defaults to sarvam-30b unless the user sets SARVAM_CHAT_MODEL).
    MODEL_OVERRIDE: Optional[str] = None
    PROVIDER_NAME: str = "sarvam"

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def mock_mode(self) -> bool:
        return not settings.sarvam_api_key

    @property
    def model(self) -> str:
        return self.MODEL_OVERRIDE or settings.sarvam_chat_model

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.PROVIDER_NAME,
            display_name=f"Sarvam {self.model}",
            is_sovereign=True,
            is_mock=self.mock_mode,
            model_id=self.model,
            base_url=settings.sarvam_base_url,
        )

    async def _client_ref(self) -> httpx.AsyncClient:
        if self._client is None:
            from ..http_client import httpx_client_kwargs
            self._client = httpx.AsyncClient(
                base_url=settings.sarvam_base_url,
                headers={
                    "api-subscription-key": settings.sarvam_api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, read=60.0),
                **httpx_client_kwargs(),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # Phase 6d â€” stop sequences for chain-of-thought patterns. Kept VERY
    # tight (only the most distinctive markers). The post-stream
    # `detect_and_strip_reasoning` scrubber is the safety net for anything
    # that gets through. Removed `</think>` â€” it can appear in legit
    # `content` and would terminate immediately.
    _STOP_SEQUENCES = [
        "**Draft 1:", "**Draft 2:", "**Draft 3:",
        "*Initial draft:*",
    ]

    async def chat_stream(
        self, *, messages, model=None, temperature=0.3, max_tokens=3500,
    ) -> AsyncIterator[str]:
        """Return Sarvam chat text through the streaming interface.

        Sarvam's SSE stream can emit only reasoning_content and finish before
        any message content arrives. The non-streaming endpoint returns the
        final content reliably, so we use it here and yield the final answer as
        one chunk. The orchestrator still treats this as a stream, but citizens
        get the usable answer instead of an empty/chain-of-thought fallback.
        """
        if self.mock_mode:
            raise RuntimeError("SarvamProvider.chat_stream called in mock mode "
                               "— the factory should have substituted MockProvider")
        client = await self._client_ref()
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Deliberately non-streaming; see docstring above.
            "stream": False,
            "stop": self._STOP_SEQUENCES,
        }
        try:
            sys_msg = next((m for m in messages if m.get("role") == "system"), {})
            usr_msg = messages[-1] if messages else {}
            log.info(
                "Sarvam call: model=%s temp=%s max_tokens=%s msgs=%d "
                "sys_chars=%d usr_chars=%d usr_role=%s stream=false",
                payload["model"], temperature, max_tokens, len(messages),
                len(sys_msg.get("content", "")),
                len(str(usr_msg.get("content", ""))),
                usr_msg.get("role"),
            )
        except Exception:
            pass

        resp = await client.post("/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            body_text = resp.text[:500]
            log.error(
                "Sarvam chat HTTP %d for model=%s temp=%s max_tokens=%s msg_count=%d "
                "first_msg_role=%s. Body: %s",
                resp.status_code, payload["model"], temperature, max_tokens,
                len(messages), (messages[0].get("role") if messages else "?"),
                body_text,
            )
            raise RuntimeError(f"Sarvam chat HTTP {resp.status_code}: {body_text}")

        obj = resp.json()
        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError(f"Sarvam chat returned no choices. Keys: {list(obj.keys())}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if content:
            yield content
            return

        reasoning = message.get("reasoning_content") or ""
        if reasoning:
            log.warning(
                "Sarvam non-streaming response had no content; yielding reasoning fallback "
                "for scrubber. reasoning_chars=%d finish_reason=%s msg_count=%d",
                len(reasoning), choice.get("finish_reason"), len(messages),
            )
            yield reasoning
            return

        log.warning(
            "Sarvam non-streaming response had no content or reasoning. finish_reason=%s msg_count=%d",
            choice.get("finish_reason"), len(messages),
        )

    async def chat_complete(
        self, *, messages, model=None, temperature=0.3, max_tokens=3500,
        json_mode=False,
    ) -> str:
        if self.mock_mode:
            raise RuntimeError("SarvamProvider.chat_complete called in mock mode")
        client = await self._client_ref()
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Phase 6d â€” reasoning_effort omitted; see chat_stream comment.
            "stop": self._STOP_SEQUENCES,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await client.post("/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            body = resp.text[:300]
            log.error("Sarvam chat HTTP %d: %s", resp.status_code, body)
            raise RuntimeError(f"Sarvam chat HTTP {resp.status_code}: {body}")
        obj = resp.json()
        # Defensive: Sarvam may return {} on weird inputs, or choices may be empty
        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError(f"Sarvam chat returned no choices. Keys: {list(obj.keys())}")
        choice = choices[0].get("message", {})
        # Phase 6c â€” prefer real content; fall back to reasoning ONLY as a
        # last resort (the orchestrator's reasoning-leak scrubber will then
        # strip any chain-of-thought patterns from that fallback).
        return choice.get("content") or choice.get("reasoning_content") or ""

    @property
    def supports_tools(self) -> bool:
        return not self.mock_mode

    async def chat_with_tools(
        self, *, messages, tools, model=None, temperature=0.2,
        max_tokens=800, tool_choice="auto",
    ) -> dict:
        """OpenAI-style function-calling. Sarvam's /v1/chat/completions accepts
        `tools` + `tool_choice` and returns `message.tool_calls`. We parse the
        JSON-string arguments into a dict so the caller can execute directly."""
        if self.mock_mode:
            return {"content": "", "tool_calls": []}
        client = await self._client_ref()
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            body = resp.text[:300]
            log.error("Sarvam tool-call HTTP %d: %s", resp.status_code, body)
            raise RuntimeError(f"Sarvam tool-call HTTP {resp.status_code}: {body}")
        choices = (resp.json() or {}).get("choices") or []
        if not choices:
            return {"content": "", "tool_calls": []}
        msg = choices[0].get("message", {}) or {}
        parsed = []
        for c in (msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            parsed.append({"id": c.get("id"), "name": fn.get("name"),
                           "arguments": args if isinstance(args, dict) else {}})
        return {"content": msg.get("content") or "", "tool_calls": parsed}


# ---------------------------------------------------------------------------
# Phase 6d â€” Sarvam-105B variant
# ---------------------------------------------------------------------------
# Per the Sarvam chat skill: 105B has 128K context and is best for "complex
# reasoning, coding, agentic workflows". 30B is best for "real-time chat,
# voice agents, conversational AI". Most agents stay on 30B for snappier
# replies; pin 105B on the CMO agent or the coordinator's grievance flow
# where multi-step reasoning helps.

class Sarvam105BProvider(SarvamProvider):
    MODEL_OVERRIDE = "sarvam-105b"
    PROVIDER_NAME = "sarvam-105b"


class Sarvam30BProvider(SarvamProvider):
    """Explicit 30B class so the admin UI can show it next to 105B."""
    MODEL_OVERRIDE = "sarvam-30b"
    PROVIDER_NAME = "sarvam-30b"

