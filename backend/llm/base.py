"""LLM provider — abstract base + shared types."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class ProviderInfo:
    """What the simulator's status pill + admin console show."""
    name: str                # 'sarvam', 'openai', etc.
    display_name: str        # 'Sarvam-30B', 'GPT-4o-mini', etc.
    is_sovereign: bool       # True for Sarvam, False for overseas providers
    is_mock: bool
    model_id: str
    base_url: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "display_name": self.display_name,
            "is_sovereign": self.is_sovereign, "is_mock": self.is_mock,
            "model_id": self.model_id, "base_url": self.base_url,
        }


class LLMProvider(abc.ABC):
    """Minimum interface every LLM provider must implement."""

    @abc.abstractmethod
    async def chat_stream(
        self, *,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 800,
    ) -> AsyncIterator[str]:
        """Yield text chunks as the LLM produces them."""
        ...

    @abc.abstractmethod
    async def chat_complete(
        self, *,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        json_mode: bool = False,
    ) -> str:
        """Non-streaming completion. Returns the full response text."""
        ...

    @property
    @abc.abstractmethod
    def mock_mode(self) -> bool:
        """True when the provider is missing required credentials and would
        not actually make a real API call."""
        ...

    @abc.abstractmethod
    def info(self) -> ProviderInfo:
        """Provider metadata for health endpoints + UI."""
        ...

    async def chat_with_tools(
        self, *,
        messages: list[dict],
        tools: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        tool_choice: str = "auto",
    ) -> dict:
        """Function-calling: hand the model a list of tool schemas and let it
        decide whether to call one.

        Returns ``{"content": str, "tool_calls": [{"id", "name", "arguments": dict}]}``.

        Default implementation returns no tool calls — providers that don't
        support OpenAI-style function-calling (or the mock provider) simply
        never select a tool, and the caller falls back to keyword matching.
        """
        return {"content": "", "tool_calls": []}

    @property
    def supports_tools(self) -> bool:
        """Whether this provider can do real function-calling."""
        return False

    async def close(self) -> None:
        """Optional cleanup hook."""
        pass


# ---------------------------------------------------------------------------
# Helpers shared across providers
# ---------------------------------------------------------------------------

def extract_system_and_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Pull out a top-level system message and return (system, rest).

    Anthropic and Gemini both want the system instruction OUTSIDE the
    messages array, so providers that target those APIs use this helper.
    OpenAI-compatible providers can leave messages alone.
    """
    if not messages:
        return "", []
    if messages[0].get("role") == "system":
        return messages[0].get("content", ""), messages[1:]
    return "", messages
