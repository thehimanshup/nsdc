"""Admin-side routes that expose Sarvam diagnostics.

Lets the admin console's Sarvam tab run live tests and see the actual
API responses (instead of silently falling back to mock).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import settings
from .sarvam_diagnostics import (
    check_chat, check_stt, check_translate, check_tts, check_vision,
    check_key_present, check_base_url, run_all,
)

log = logging.getLogger("sarvam.diag.routes")
router = APIRouter()


class TTSTestRequest(BaseModel):
    voice: str = "shubh"
    language: str = "en-IN"


@router.get("/api/v1/admin/sarvam/diagnose")
async def sarvam_diagnose() -> dict:
    """Run every Sarvam health check. Slow (~10-30 s) when LIVE; fast in MOCK."""
    return await run_all()


@router.get("/api/v1/admin/sarvam/status")
async def sarvam_status() -> dict:
    """Quick pre-flight check — no API calls. For the dashboard pill."""
    key = check_key_present()
    base = check_base_url()
    # Probe truststore availability without making any HTTP call
    try:
        import truststore   # noqa: F401
        truststore_available = True
    except ImportError:
        truststore_available = False
    from .http_client import current_strategy
    return {
        "key_present": key.status == "pass",
        "key_preview": key.request_summary.get("key_preview"),
        "base_url": settings.sarvam_base_url,
        "chat_model": settings.sarvam_chat_model,
        "mode": "LIVE" if key.status == "pass" else "MOCK",
        "ready": key.status == "pass" and base.status == "pass",
        "ssl": {
            "verify": settings.sarvam_verify_ssl,
            "ca_bundle": settings.sarvam_ca_bundle or None,
            "truststore_installed": truststore_available,
            "strategy": current_strategy(),
        },
    }


@router.post("/api/v1/admin/sarvam/test-chat")
async def sarvam_test_chat() -> dict:
    return (await check_chat()).as_dict()


@router.post("/api/v1/admin/sarvam/test-stt")
async def sarvam_test_stt() -> dict:
    return (await check_stt()).as_dict()


@router.post("/api/v1/admin/sarvam/test-tts")
async def sarvam_test_tts(req: TTSTestRequest) -> dict:
    return (await check_tts(voice=req.voice, lang=req.language)).as_dict()


@router.post("/api/v1/admin/sarvam/test-translate")
async def sarvam_test_translate() -> dict:
    return (await check_translate()).as_dict()


@router.post("/api/v1/admin/sarvam/test-vision")
async def sarvam_test_vision() -> dict:
    return (await check_vision()).as_dict()
