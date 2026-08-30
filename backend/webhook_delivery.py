"""Webhook result delivery for the Callback Agent Platform.

When a call completes, the engine's structured result is POSTed to the
client's `webhook_url`. Enterprise integration requires this be:

  - idempotent  — `X-Callback-Id: <job_id>` so a client can dedupe retries;
  - signed      — HMAC-SHA256 over the raw body in `X-Callback-Signature`,
                  using CALLBACK_WEBHOOK_SECRET (skipped if unset, with a warn);
  - retried     — a few attempts with backoff before giving up.

A permanently-failing endpoint is logged and dropped here (the dead-letter
queue is WS-5). Failure-isolation: delivery problems never crash the worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os

import httpx

from .http_client import httpx_client_kwargs

log = logging.getLogger("callback.webhook")

def _max_attempts() -> int:
    return int(os.getenv("CALLBACK_WEBHOOK_RETRIES", "4"))


def _timeout() -> float:
    return float(os.getenv("CALLBACK_WEBHOOK_TIMEOUT", "10"))


def _sign(body: bytes) -> str | None:
    secret = os.getenv("CALLBACK_WEBHOOK_SECRET", "")
    if not secret:
        return None
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(webhook_url: str, job_id: str, result: dict) -> bool:
    """POST `result` to `webhook_url`. Returns True on a 2xx, else False after
    exhausting retries. Never raises."""
    if not webhook_url:
        log.warning("Callback %s has no webhook_url — skipping delivery", job_id)
        return False

    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Callback-Id": job_id}
    sig = _sign(body)
    if sig:
        headers["X-Callback-Signature"] = f"sha256={sig}"
    else:
        log.warning("CALLBACK_WEBHOOK_SECRET unset — delivering %s UNSIGNED", job_id)

    max_attempts = _max_attempts()
    delay = 1.0
    async with httpx.AsyncClient(timeout=_timeout(), **httpx_client_kwargs()) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(webhook_url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    log.info("Delivered callback %s → %s (attempt %d)",
                             job_id, webhook_url, attempt)
                    return True
                log.warning("Callback %s delivery got HTTP %d (attempt %d/%d)",
                            job_id, resp.status_code, attempt, max_attempts)
            except Exception as e:                # noqa: BLE001 — retryable
                log.warning("Callback %s delivery error (attempt %d/%d): %s",
                            job_id, attempt, max_attempts, e)
            if attempt < max_attempts:
                await asyncio.sleep(delay)
                delay *= 2                        # exponential backoff
    log.error("Callback %s delivery FAILED after %d attempts → dead-letter queue",
              job_id, max_attempts)
    _to_dead_letter(webhook_url, job_id, result)
    return False


def _dlq_path():
    from pathlib import Path

    from .config import settings
    return Path(settings.data_dir) / "callback_dlq.jsonl"


def _to_dead_letter(webhook_url: str, job_id: str, result: dict) -> None:
    """Persist an undeliverable result to data/callback_dlq.jsonl for later
    replay. A failing endpoint must never silently lose a verification result."""
    try:
        path = _dlq_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"job_id": job_id, "webhook_url": webhook_url,
                           "result": result}, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                            # noqa: BLE001
        log.exception("Failed to write callback %s to DLQ", job_id)


def list_dead_letters() -> list[dict]:
    """Read the dead-letter queue (undeliverable results awaiting replay)."""
    path = _dlq_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:                        # noqa: BLE001 — skip a corrupt line
            continue
    return out


async def replay_dead_letters() -> dict:
    """Re-attempt delivery for every DLQ entry. Successfully delivered entries
    are dropped; still-failing ones are re-written (and re-dead-lettered). The
    file is truncated first so a redelivery can't be double-counted."""
    entries = list_dead_letters()
    if not entries:
        return {"replayed": 0, "delivered": 0, "still_failing": 0}
    try:
        _dlq_path().unlink()
    except Exception:                            # noqa: BLE001
        pass
    delivered = 0
    for e in entries:
        ok = await deliver(e.get("webhook_url", ""), e.get("job_id", ""),
                           e.get("result") or {})
        if ok:
            delivered += 1
    still = len(entries) - delivered
    log.info("DLQ replay: %d delivered, %d still failing", delivered, still)
    return {"replayed": len(entries), "delivered": delivered, "still_failing": still}
