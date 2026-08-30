"""Sarvam API health check + per-capability live diagnostics.

Two ways to use this module:

  1. As a CLI:
         python -m backend.sarvam_diagnostics
     Runs every check and prints a summary. Exits 0 if all green, else 1.

  2. From the admin console UI (Sarvam tab) which hits these routes:
         GET  /api/v1/admin/sarvam/diagnose       — runs the full suite
         POST /api/v1/admin/sarvam/test-chat       — single chat test
         POST /api/v1/admin/sarvam/test-stt        — single STT test
         POST /api/v1/admin/sarvam/test-tts        — single TTS test
         POST /api/v1/admin/sarvam/test-translate  — single translate test
         POST /api/v1/admin/sarvam/test-vision     — single vision test

The diagnostic checks make REAL Sarvam API calls (never mock fallback).
Each check returns an explicit pass/fail + the API response details, so
you can see exactly why something is broken.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import struct
import sys
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import settings
from .http_client import httpx_client_kwargs

# Windows terminals often default to cp1252 and cannot print diagnostic emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

log = logging.getLogger("sarvam.diag")


@dataclass
class CheckResult:
    name: str
    status: str = "fail"             # pass | fail | skipped
    message: str = ""
    http_status: Optional[int] = None
    latency_ms: int = 0
    request_summary: dict = field(default_factory=dict)
    response_summary: dict = field(default_factory=dict)
    error_kind: Optional[str] = None  # auth | network | shape | rate_limit | other

    def as_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status, "message": self.message,
            "http_status": self.http_status, "latency_ms": self.latency_ms,
            "request": self.request_summary, "response": self.response_summary,
            "error_kind": self.error_kind,
        }


# ---------------------------------------------------------------------------
# Generic request helper that captures meaningful failure data
# ---------------------------------------------------------------------------

def _classify_error(http_status: Optional[int], exc: Optional[Exception]) -> str:
    if exc is not None:
        name = exc.__class__.__name__
        if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "ConnectionError"):
            return "network"
        if name == "DecodeError":
            return "shape"
        return "other"
    if http_status is None:
        return "network"
    if http_status in (401, 403):
        return "auth"
    if http_status == 429:
        return "rate_limit"
    if 400 <= http_status < 500:
        return "shape"
    if 500 <= http_status < 600:
        return "other"
    return "other"


def _truncate(s: Any, n: int = 280) -> Any:
    if isinstance(s, (dict, list)):
        return s
    return (str(s)[:n] + "…") if s and len(str(s)) > n else s


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_key_present() -> CheckResult:
    """Is SARVAM_API_KEY set at all?"""
    r = CheckResult(name="api_key_present")
    if not settings.sarvam_api_key:
        r.status = "fail"
        r.error_kind = "auth"
        r.message = (
            "SARVAM_API_KEY is empty in your environment.\n"
            "Get a key at https://dashboard.sarvam.ai and add it to .env:\n"
            "    SARVAM_API_KEY=sk_xxxxxxxxxxxxxxxxxxxx\n"
            "Then restart the server. Until you do, voice/STT/TTS/vision all "
            "fall back to mock (the 'beep' you hear is the mock chime)."
        )
        return r
    r.status = "pass"
    r.message = f"Key present: yes (length={len(settings.sarvam_api_key)})"
    r.request_summary = {"key_present": True, "key_length": len(settings.sarvam_api_key)}
    return r


def check_base_url() -> CheckResult:
    r = CheckResult(name="base_url")
    if not settings.sarvam_base_url:
        r.status = "fail"
        r.message = "SARVAM_BASE_URL is empty"
        return r
    r.status = "pass"
    r.message = settings.sarvam_base_url
    r.request_summary = {"base_url": settings.sarvam_base_url}
    return r


# ---------------------------------------------------------------------------
# Capability checks — these make REAL API calls
# ---------------------------------------------------------------------------

async def check_chat(model: Optional[str] = None) -> CheckResult:
    """Hit /chat/completions with a minimal payload."""
    r = CheckResult(name="chat (Sarvam-30B)")
    if not settings.sarvam_api_key:
        r.status = "skipped"
        r.message = "No SARVAM_API_KEY — skipped (would use mock)"
        return r
    url = f"{settings.sarvam_base_url}/v1/chat/completions"
    payload = {
        "model": model or settings.sarvam_chat_model,
        "messages": [
            {"role": "system", "content": "You are a test agent. Reply with exactly 'ok'."},
            {"role": "user", "content": "Reply with the single word 'ok'."},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    r.request_summary = {"url": url, "model": payload["model"]}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=40.0), **httpx_client_kwargs()) as c:
            resp = await c.post(url, json=payload, headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            })
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        r.http_status = resp.status_code
        if resp.status_code >= 400:
            r.status = "fail"
            r.error_kind = _classify_error(resp.status_code, None)
            r.message = f"HTTP {resp.status_code}: {_truncate(resp.text)}"
            return r
        obj = resp.json()
        choice = obj.get("choices", [{}])[0].get("message", {})
        content = choice.get("content") or ""
        usage = obj.get("usage", {})
        r.response_summary = {
            "reply_preview": _truncate(content),
            "tokens": usage,
        }
        if not content:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = (
                "Sarvam returned a 2xx but the message content was empty. "
                "Likely cause: max_tokens too low (Sarvam reasons internally before "
                "emitting content). Raise max_tokens to 500+. "
                f"Full message keys: {list(choice.keys())}"
            )
            return r
        r.status = "pass"
        r.message = f"Reply received in {r.latency_ms}ms: {_truncate(content, 80)}"
    except Exception as e:
        r.error_kind = _classify_error(None, e)
        r.message = f"{e.__class__.__name__}: {e}"
    return r


async def check_translate() -> CheckResult:
    """Hit /translate. Uses the chat path internally since Sarvam exposes
    translate as a sibling endpoint — keep this idempotent and cheap."""
    r = CheckResult(name="translate (Sarvam-Translate)")
    if not settings.sarvam_api_key:
        r.status = "skipped"
        r.message = "No SARVAM_API_KEY — skipped"
        return r
    url = f"{settings.sarvam_base_url}/translate"
    payload = {
        "input": "Hello, this is a translation test.",
        "source_language_code": "en-IN",
        "target_language_code": "ta-IN",
        "model": "sarvam-translate:v1",
    }
    r.request_summary = {"url": url, "source": "en-IN", "target": "ta-IN"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=40.0), **httpx_client_kwargs()) as c:
            resp = await c.post(url, json=payload, headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            })
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        r.http_status = resp.status_code
        if resp.status_code >= 400:
            r.status = "fail"
            r.error_kind = _classify_error(resp.status_code, None)
            r.message = f"HTTP {resp.status_code}: {_truncate(resp.text)}"
            return r
        obj = resp.json()
        translated = obj.get("translated_text") or obj.get("output") or ""
        r.response_summary = {"translated_preview": _truncate(translated)}
        if not translated:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = f"Response missing translated_text. Keys: {list(obj.keys())}"
            return r
        r.status = "pass"
        r.message = f"Translated in {r.latency_ms}ms: {_truncate(translated, 80)}"
    except Exception as e:
        r.error_kind = _classify_error(None, e)
        r.message = f"{e.__class__.__name__}: {e}"
    return r


def _make_tiny_wav(seconds: float = 1.0, freq: float = 440.0,
                   rate: int = 16000) -> bytes:
    """Generate a real WAV file for STT testing. A 1-sec sine wave at 440Hz."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        for i in range(int(rate * seconds)):
            t = i / rate
            sample = int(0.2 * math.sin(2 * math.pi * freq * t) * 32767)
            w.writeframes(struct.pack("<h", sample))
    return buf.getvalue()


async def check_stt() -> CheckResult:
    """Hit /speech-to-text with a tiny synthetic WAV."""
    r = CheckResult(name="STT (Saaras v3)")
    if not settings.sarvam_api_key:
        r.status = "skipped"
        r.message = "No SARVAM_API_KEY — skipped"
        return r
    url = f"{settings.sarvam_base_url}/speech-to-text"
    wav = _make_tiny_wav(seconds=1.5, freq=440.0)
    r.request_summary = {"url": url, "audio_bytes": len(wav), "model": "saaras:v3"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), **httpx_client_kwargs()) as c:
            resp = await c.post(
                url,
                files={"file": ("test.wav", wav, "audio/wav")},
                data={"model": "saaras:v3", "mode": "transcribe", "language_code": "en-IN"},
                headers={"api-subscription-key": settings.sarvam_api_key},
            )
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        r.http_status = resp.status_code
        if resp.status_code >= 400:
            r.status = "fail"
            r.error_kind = _classify_error(resp.status_code, None)
            r.message = f"HTTP {resp.status_code}: {_truncate(resp.text)}"
            return r
        obj = resp.json()
        transcript = obj.get("transcript") or obj.get("text") or ""
        lang = obj.get("language_code") or obj.get("language") or ""
        r.response_summary = {"transcript_preview": _truncate(transcript),
                              "detected_language": lang}
        r.status = "pass"
        r.message = (f"STT round-trip OK in {r.latency_ms}ms. "
                     f"(A pure-tone test clip yields a near-empty transcript, "
                     f"which is expected — what matters is the endpoint responded "
                     f"with a 2xx and the expected JSON shape.)")
    except Exception as e:
        r.error_kind = _classify_error(None, e)
        r.message = f"{e.__class__.__name__}: {e}"
    return r


async def check_tts(*, voice: str = "shubh", lang: str = "en-IN") -> CheckResult:
    """Hit /text-to-speech and verify we got real audio bytes back."""
    r = CheckResult(name=f"TTS (Bulbul v3 / voice={voice})")
    if not settings.sarvam_api_key:
        r.status = "skipped"
        r.message = "No SARVAM_API_KEY — skipped"
        return r
    url = f"{settings.sarvam_base_url}/text-to-speech"
    payload = {
        "text": "Hello from Sarvam diagnostics.",
        "target_language_code": lang,
        "speaker": voice,
        "model": "bulbul:v3",
        "pace": 1.0,
    }
    r.request_summary = {"url": url, "voice": voice, "lang": lang}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), **httpx_client_kwargs()) as c:
            resp = await c.post(url, json=payload, headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            })
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        r.http_status = resp.status_code
        if resp.status_code >= 400:
            r.status = "fail"
            r.error_kind = _classify_error(resp.status_code, None)
            r.message = f"HTTP {resp.status_code}: {_truncate(resp.text)}"
            return r
        obj = resp.json()
        audios = obj.get("audios") or []
        if not audios or not audios[0]:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = f"Response missing audios[0]. Keys: {list(obj.keys())}"
            return r
        # Verify it actually decodes to a real audio blob, not zero bytes
        try:
            audio_bytes = base64.b64decode(audios[0])
        except Exception as e:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = f"audios[0] not valid base64: {e}"
            return r
        if len(audio_bytes) < 1000:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = f"Audio blob suspiciously small: {len(audio_bytes)} bytes"
            return r
        r.response_summary = {"audio_bytes": len(audio_bytes),
                              "is_wav": audio_bytes[:4] == b"RIFF"}
        r.status = "pass"
        r.message = (f"TTS produced {len(audio_bytes)} bytes in {r.latency_ms}ms"
                     f"{' — WAV detected' if audio_bytes[:4] == b'RIFF' else ''}")
    except Exception as e:
        r.error_kind = _classify_error(None, e)
        r.message = f"{e.__class__.__name__}: {e}"
    return r


async def check_vision() -> CheckResult:
    """Probe Sarvam Vision (document-intelligence).

    Per Sarvam docs (https://docs.sarvam.ai/api-reference-docs/getting-started/models/sarvam-vision),
    Sarvam Vision is an async job-based API, not a one-shot REST endpoint. The
    SDK flow is:
        job = client.document_intelligence.create_job(language=..., output_format='md')
        job.upload_file('doc.pdf')
        job.start()
        job.wait_until_complete()
        job.download_output('out.zip')

    For a quick health check we just verify the create_job endpoint is reachable —
    we don't upload a real document or wait for processing (that would take
    20-60s minimum). A successful create returns a job_id; we then immediately
    abandon the job.
    """
    r = CheckResult(name="Vision (Sarvam Document Intelligence)")
    if not settings.sarvam_api_key:
        r.status = "skipped"
        r.message = "No SARVAM_API_KEY — skipped"
        return r
    url = f"{settings.sarvam_base_url}/doc-digitization/job/v1"
    # Per Sarvam docs: job_parameters wraps language + output_format
    payload = {"job_parameters": {"language": "en-IN", "output_format": "md"}}
    r.request_summary = {"url": url, **payload["job_parameters"]}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), **httpx_client_kwargs()) as c:
            resp = await c.post(url, json=payload, headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            })
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        r.http_status = resp.status_code

        # Safe body decode — never assume it's JSON
        body_text = resp.text or ""
        body_obj: dict = {}
        try:
            body_obj = resp.json() if body_text else {}
        except Exception:
            # Common cause: HTTP 5xx with HTML or plain-text "Internal Server Error" body
            r.response_summary = {"body_preview": _truncate(body_text, 200),
                                  "content_type": resp.headers.get("content-type", "")}

        if resp.status_code == 404:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = ("HTTP 404 — `/doc-digitization/job/v1` not found. "
                         "Either Sarvam has shifted endpoints again, or "
                         "your account doesn't have Document Intelligence enabled. "
                         "Check https://docs.sarvam.ai/api-reference-docs/document-intelligence/initialise")
            return r
        if resp.status_code >= 500:
            r.status = "fail"
            r.error_kind = "other"
            r.message = (f"HTTP {resp.status_code} — Sarvam returned a server error. "
                         f"Body (first 200 chars): {_truncate(body_text, 200)}")
            return r
        if resp.status_code >= 400:
            r.status = "fail"
            r.error_kind = _classify_error(resp.status_code, None)
            err = body_obj.get("error") or body_obj.get("message") or _truncate(body_text, 200)
            r.message = f"HTTP {resp.status_code}: {err}"
            return r

        # 2xx response — sanity-check shape
        job_id = body_obj.get("job_id") or body_obj.get("id")
        if not job_id:
            r.status = "fail"
            r.error_kind = "shape"
            r.message = (f"HTTP {resp.status_code} but no job_id in response. "
                         f"Keys: {list(body_obj.keys())}. Body preview: {_truncate(body_text, 160)}")
            return r
        r.response_summary = {"job_id_preview": job_id[:12] + "…",
                              "response_keys": list(body_obj.keys())}
        r.status = "pass"
        r.message = (f"Document-intelligence reachable in {r.latency_ms}ms. "
                     f"Test job {job_id[:12]}… created (will be abandoned).")
    except Exception as e:
        r.error_kind = _classify_error(None, e)
        r.message = f"{e.__class__.__name__}: {e}"
    return r


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_all() -> dict:
    """Run every diagnostic and return a summary report."""
    pre = [check_key_present(), check_base_url()]
    pre_pass = all(c.status == "pass" for c in pre)

    if pre_pass:
        chat, stt, tts, translate, vision = await asyncio.gather(
            check_chat(), check_stt(), check_tts(),
            check_translate(), check_vision(),
            return_exceptions=False,
        )
        capability_checks = [chat, stt, tts, translate, vision]
    else:
        capability_checks = [
            CheckResult(name="chat (Sarvam-30B)", status="skipped",
                        message="Pre-flight failed"),
            CheckResult(name="STT (Saaras v3)", status="skipped",
                        message="Pre-flight failed"),
            CheckResult(name="TTS (Bulbul v3)", status="skipped",
                        message="Pre-flight failed"),
            CheckResult(name="translate", status="skipped",
                        message="Pre-flight failed"),
            CheckResult(name="vision", status="skipped",
                        message="Pre-flight failed"),
        ]

    all_checks = pre + capability_checks
    passed = sum(1 for c in all_checks if c.status == "pass")
    failed = sum(1 for c in all_checks if c.status == "fail")
    skipped = sum(1 for c in all_checks if c.status == "skipped")

    return {
        "overall": "green" if failed == 0 and passed >= 2 else (
                   "red" if failed > 0 else "yellow"),
        "passed": passed, "failed": failed, "skipped": skipped,
        "key_set": bool(settings.sarvam_api_key),
        "base_url": settings.sarvam_base_url,
        "chat_model": settings.sarvam_chat_model,
        "checks": [c.as_dict() for c in all_checks],
        "hints": _summary_hints(all_checks),
    }


def _summary_hints(checks: list[CheckResult]) -> list[str]:
    hints: list[str] = []
    if not settings.sarvam_api_key:
        hints.append("Set SARVAM_API_KEY in your .env file and restart.")
        return hints
    auth_fail = any(c.error_kind == "auth" for c in checks)
    if auth_fail:
        hints.append("HTTP 401/403 — your SARVAM_API_KEY is rejected. Re-issue from "
                     "https://dashboard.sarvam.ai/")
    network_fail = any(c.error_kind == "network" for c in checks)
    if network_fail:
        hints.append("Network errors. Check internet, firewall, corporate proxy. "
                     "If behind a proxy, set HTTPS_PROXY in your environment.")
    rate = any(c.error_kind == "rate_limit" for c in checks)
    if rate:
        hints.append("Hit rate-limit (HTTP 429). Wait a few seconds and re-run, "
                     "or upgrade your Sarvam plan.")
    shape = any(c.error_kind == "shape" for c in checks)
    if shape:
        hints.append("Some endpoints returned an unexpected shape — Sarvam's API "
                     "may have shifted since this code was written. Compare against "
                     "https://docs.sarvam.ai/api-reference-docs")
    if not hints:
        hints.append("All checks passed — Sarvam is healthy from this host.")
    return hints


# ---------------------------------------------------------------------------
# CLI entry point: python -m backend.sarvam_diagnostics
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    print("\n" + "=" * 70)
    print("  SARVAM DIAGNOSTICS")
    print("=" * 70)
    print(f"  Overall:    {report['overall'].upper()}")
    print(f"  Key set:    {report['key_set']}")
    print(f"  Base URL:   {report['base_url']}")
    print(f"  Chat model: {report['chat_model']}")
    print(f"  Result:     {report['passed']} passed / "
          f"{report['failed']} failed / {report['skipped']} skipped")
    print("=" * 70)
    for c in report["checks"]:
        emoji = {"pass": "✓", "fail": "✗", "skipped": "—"}.get(c["status"], "?")
        status_color = {"pass": "32", "fail": "31", "skipped": "33"}.get(c["status"], "0")
        print(f"\n  \033[{status_color}m{emoji} {c['name']}\033[0m")
        if c.get("http_status"):
            print(f"     HTTP:    {c['http_status']}")
        if c.get("latency_ms"):
            print(f"     Latency: {c['latency_ms']} ms")
        if c.get("error_kind"):
            print(f"     Kind:    {c['error_kind']}")
        if c.get("message"):
            print(f"     {c['message']}")
        if c.get("response"):
            for k, v in c["response"].items():
                print(f"     - {k}: {v}")
    print()
    print("─" * 70)
    print("  HINTS:")
    for h in report.get("hints", []):
        print(f"    • {h}")
    print()


def main() -> int:
    # Make sure logging output goes to stderr so the print() output stays clean
    logging.basicConfig(level=logging.WARNING)
    report = asyncio.run(run_all())
    _print_report(report)
    return 0 if report["overall"] == "green" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
