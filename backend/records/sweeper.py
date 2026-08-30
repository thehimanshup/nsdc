"""SLA escalation sweeper — Phase 6e.

A background asyncio task (started from main.py lifespan, same pattern as the
audit daily-root loop and the broadcast demo loop). Every tick it finds
records whose SLA clock has expired and auto-escalates them L1→L2→L3→L4,
exactly like the MP CM Helpline does when a level misses its deadline.

With RECORDS_SLA_DEMO=true (default), policy hours are interpreted as minutes,
so a stakeholder can watch a complaint climb the ladder live.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from ..config import settings
from .store import records_store
from . import service

log = logging.getLogger("records.sweeper")

_TICK_SECONDS = int(os.getenv("RECORDS_SWEEP_SECONDS", "20"))
_PID = os.getpid()
# A lock is "stale" (its holder died) after this many seconds without a
# heartbeat refresh, at which point another worker may take leadership.
_LEADER_TTL = _TICK_SECONDS * 3


def _lock_path() -> Path:
    return Path(settings.data_dir) / "sweeper.lock"


def _try_become_leader() -> bool:
    """Single-leader election so that with multiple uvicorn workers only ONE
    sweeper escalates records (otherwise a record gets escalated N times per
    tick). File-based: claim the lock if it's free or stale, then heartbeat."""
    p = _lock_path()
    now = time.time()
    try:
        if p.exists():
            data = json.loads(p.read_text() or "{}")
            if data.get("pid") != _PID and (now - float(data.get("ts", 0))) < _LEADER_TTL:
                return False  # another live worker holds leadership
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": _PID, "ts": now}))
        return True
    except Exception as e:
        log.debug("leader election error (assuming leader): %s", e)
        return True


async def sweep_once() -> int:
    """One pass. Returns the number of records escalated."""
    now = datetime.utcnow()
    escalated = 0
    for rec in records_store.open_sla_records():
        try:
            due = datetime.fromisoformat(rec.sla_due_at)
        except Exception:
            continue
        if due <= now:
            await service.escalate(rec, reason="SLA timer breached")
            escalated += 1
    return escalated


async def sweep_loop() -> None:
    log.info("SLA sweeper started (tick=%ds)", _TICK_SECONDS)
    # small startup delay so the app is fully up first
    await asyncio.sleep(5)
    while True:
        try:
            if _try_become_leader():       # only the leader escalates
                n = await sweep_once()
                if n:
                    log.info("SLA sweep escalated %d record(s)", n)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("SLA sweep error: %s", e)
        await asyncio.sleep(_TICK_SECONDS)
