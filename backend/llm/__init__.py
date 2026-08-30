"""LLM provider abstraction.

Single entry point: `get_llm()` returns the configured provider.
Switch providers via the LLM_PROVIDER env var (sarvam | openai | anthropic
| gemini | ollama | mock). Each provider falls back to mock-mode behaviour
when its API key/credentials are missing.

The interface (see base.LLMProvider) is intentionally narrow:
    - chat_stream(messages, ...)  -> async iterator of text chunks
    - chat_complete(messages, ..., json_mode=False)  -> str

That is everything the orchestrator + intent router need. Voice (Saaras,
Bulbul) and Vision keep using their own native Sarvam clients — those
are speech/document models, not interchangeable with frontier LLMs.

USAGE:
    from backend.llm import llm
    async for chunk in llm.chat_stream(messages=[...]):
        print(chunk, end='')
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import settings
from .base import LLMProvider, ProviderInfo
from .mock import MockProvider

log = logging.getLogger("llm")


_PROVIDER_REGISTRY: dict[str, str] = {
    "sarvam":       "backend.llm.sarvam.SarvamProvider",
    # Phase 6d — explicit Sarvam model picks so an agent can pin 30B vs 105B
    "sarvam-30b":   "backend.llm.sarvam.Sarvam30BProvider",
    "sarvam-105b":  "backend.llm.sarvam.Sarvam105BProvider",
    "openai":       "backend.llm.openai_compat.OpenAIProvider",
    "groq":         "backend.llm.openai_compat.GroqProvider",
    "together":     "backend.llm.openai_compat.TogetherProvider",
    "mistral":      "backend.llm.openai_compat.MistralProvider",
    "anthropic":    "backend.llm.anthropic.AnthropicProvider",
    "gemini":       "backend.llm.gemini.GeminiProvider",
    "ollama":       "backend.llm.openai_compat.OllamaProvider",
    "mock":         "backend.llm.mock.MockProvider",
}


def list_providers() -> list[str]:
    return list(_PROVIDER_REGISTRY.keys())


def _import_provider(dotted: str):
    module_path, _, class_name = dotted.rpartition(".")
    mod = __import__(module_path, fromlist=[class_name])
    return getattr(mod, class_name)


def _build_provider(name: str) -> LLMProvider:
    """Construct the provider, falling back to mock when keys missing."""
    name = (name or "sarvam").lower()
    if name not in _PROVIDER_REGISTRY:
        if settings.is_production:
            raise RuntimeError(f"Unknown LLM_PROVIDER={name!r} in production")
        log.warning("Unknown LLM_PROVIDER=%s; falling back to sarvam.", name)
        name = "sarvam"

    try:
        cls = _import_provider(_PROVIDER_REGISTRY[name])
    except Exception as e:
        log.error("Failed to import provider %s: %s. Falling back to mock.", name, e)
        return MockProvider(provider_tag=name)

    try:
        instance = cls()
    except Exception as e:
        log.error("Failed to construct provider %s: %s. Falling back to mock.", name, e)
        return MockProvider(provider_tag=name)

    if instance.mock_mode:
        log.warning("Provider %s configured but credentials missing — using MOCK fallback.", name)
        return MockProvider(provider_tag=name, agent_mock_proxy=instance)

    return instance


# Singleton — built lazily on first access. Re-import the module after
# changing env vars to refresh.
_llm_singleton: Optional[LLMProvider] = None

# Phase 6b — per-agent LLM provider override cache. Keyed by normalised
# provider name (lowercase). Each entry is built once and reused.
_PROVIDER_CACHE: dict[str, LLMProvider] = {}


def get_llm() -> LLMProvider:
    global _llm_singleton
    if _llm_singleton is None:
        _llm_singleton = _build_provider(settings.llm_provider)
    return _llm_singleton


def get_llm_for(provider_name: Optional[str]) -> LLMProvider:
    """Phase 6b — return a provider instance, building one per name.

    If `provider_name` is None / empty / "default", falls back to the
    platform default returned by `get_llm()`. Otherwise builds (and caches)
    the named provider. Falls back to mock if construction fails or
    credentials are missing.

    Used by the orchestrator so each agent can pin its own LLM:
        Agent(id="cmo", llm_provider="sarvam")    # 105B for grievances
        Agent(id="ration", llm_provider="sarvam")  # 30B for FAQ-y
        Agent(id="dev", llm_provider="ollama")     # local for offline demo
    """
    # A global LLM_PROVIDER=mock must override per-agent pins. This keeps
    # parity/smoke tests fully offline even when agents are configured for
    # Sarvam-105B or another live model.
    if settings.llm_provider == "mock":
        return get_llm()
    if not provider_name or provider_name.strip().lower() in ("", "default", "platform"):
        return get_llm()
    name = provider_name.strip().lower()
    if name not in _PROVIDER_REGISTRY:
        log.warning("get_llm_for: unknown provider '%s', falling back to default", name)
        return get_llm()
    cached = _PROVIDER_CACHE.get(name)
    if cached is not None:
        return cached
    instance = _build_provider(name)
    _PROVIDER_CACHE[name] = instance
    log.info("get_llm_for: built provider '%s' (mock=%s)", name, instance.mock_mode)
    return instance


def reload_llm() -> LLMProvider:
    """Force-rebuild the LLM provider (e.g., after live env-var change).

    Also flushes the per-agent cache so changes to env vars are picked up.
    """
    global _llm_singleton
    _llm_singleton = None
    _PROVIDER_CACHE.clear()
    return get_llm()


# Convenient module-level proxy so callers can do `from backend.llm import llm`
# and call llm.chat_stream(...). __getattr__ dispatches to the singleton.
class _LLMProxy:
    def __getattr__(self, name):
        return getattr(get_llm(), name)


llm = _LLMProxy()


__all__ = ["get_llm", "get_llm_for", "reload_llm", "list_providers", "llm",
           "LLMProvider", "ProviderInfo", "MockProvider"]
