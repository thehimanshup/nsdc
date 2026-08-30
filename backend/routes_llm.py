"""LLM provider inspection + runtime swap.

Endpoints:
  GET  /api/v1/llm/info        — current provider details
  GET  /api/v1/llm/providers   — list all configured providers + their info
  POST /api/v1/llm/switch      — change LLM_PROVIDER without restart

In production these would sit behind admin-only auth. Phase 4b leaves
them open since the whole stack is local-dev.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .config import settings
from .auth import require_admin
from .llm import get_llm, list_providers, reload_llm
from .llm.base import ProviderInfo

log = logging.getLogger("llm.routes")

router = APIRouter()


@router.get("/api/v1/llm/info")
async def llm_info() -> dict:
    return get_llm().info().as_dict()


@router.get("/api/v1/llm/providers")
async def llm_providers() -> dict:
    """List every provider the registry knows about, with each one's
    inferred mock/live status based on currently-set env vars.
    """
    out: list[dict] = []
    for name in list_providers():
        # Temporarily build each provider to inspect its state. We
        # construct directly (not via factory) so we don't accidentally
        # swap the global singleton.
        try:
            info = _probe_provider(name)
            out.append(info.as_dict())
        except Exception as e:
            out.append({"name": name, "error": str(e)})
    return {
        "active": get_llm().info().as_dict(),
        "available": out,
    }


def _probe_provider(name: str) -> ProviderInfo:
    """Build a fresh provider instance (without setting it active) and
    return its info."""
    from .llm import _PROVIDER_REGISTRY, _import_provider
    cls = _import_provider(_PROVIDER_REGISTRY[name])
    return cls().info()


class SwitchRequest(BaseModel):
    provider: str


@router.post("/api/v1/llm/switch")
async def llm_switch(req: SwitchRequest, _admin: None = Depends(require_admin)) -> dict:
    provider = req.provider.strip().lower()
    if provider not in list_providers():
        raise HTTPException(400, f"unknown provider: {req.provider}")
    if settings.is_production and provider == "mock":
        raise HTTPException(403, "mock provider is forbidden in production")
    import os
    os.environ["LLM_PROVIDER"] = provider
    # Reload settings (re-instantiate the dataclass)
    settings.llm_provider = provider
    new_llm = reload_llm()
    info = new_llm.info()
    log.info("LLM provider switched to %s (mock=%s)", info.name, info.is_mock)
    return {"ok": True, "active": info.as_dict()}
