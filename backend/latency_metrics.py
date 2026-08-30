"""Per-turn latency metrics — Phase 6h.

Captures the delay at every hop of a conversation turn so operators can see
WHERE the lag is (STT? retrieval? LLM first token? TTS? dispatch?), per
agent and per channel, on the admin dashboard.

Design:
  - `stage()` context manager times a block and stores it in a per-turn dict.
  - `note_stage(conv_id, name, ms)` lets earlier pipeline steps (e.g. STT in
    handle_citizen_voice) stash a timing that the turn recorder later merges.
  - `record_turn(...)` appends one event to an in-memory ring buffer (fast
    dashboard queries) AND to data/latency/turns.jsonl (survives restarts;
    the last file tail is reloaded on boot).
  - `summary()` aggregates avg / p50 / p95 per stage, per agent, per channel
    over a time window.

Stages (all milliseconds, any may be absent):
  stt          speech-to-text (voice notes / calls)
  rag          corpus retrieval
  tool         tool detection + execution
  llm_first    request -> first streamed token (perceived responsiveness)
  llm_total    full LLM stream
  post         safety scans, transliteration, anti-repeat guards
  tts          text-to-speech synthesis
  total        user message in -> agent message dispatched
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from .config import settings

_LOCK = threading.RLock()
_RING: deque[dict] = deque(maxlen=2000)
_PENDING_STAGES: dict[str, dict[str, float]] = {}   # conv_id -> {stage: ms}

STAGE_ORDER = ["stt", "rag", "tool", "llm_first", "llm_total", "post",
               "tts", "total"]

STAGE_LABELS = {
    "stt": "Speech-to-text",
    "rag": "Retrieval (RAG)",
    "tool": "Tool execution",
    "llm_first": "LLM first token",
    "llm_total": "LLM full reply",
    "post": "Post-processing",
    "tts": "Text-to-speech",
    "total": "End-to-end",
}


def _path() -> Path:
    p = Path(settings.data_dir) / "latency"
    p.mkdir(parents=True, exist_ok=True)
    return p / "turns.jsonl"


def load_recent(max_rows: int = 2000) -> int:
    """Reload the tail of the JSONL file into the ring buffer on boot."""
    f = _path()
    if not f.exists():
        return 0
    try:
        lines = f.read_text(encoding="utf-8").splitlines()[-max_rows:]
    except Exception:
        return 0
    n = 0
    with _LOCK:
        _RING.clear()   # replace, never append — avoids double-counting
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                _RING.append(json.loads(line))
                n += 1
            except Exception:
                continue
    return n


@contextmanager
def stage(stages: dict, name: str) -> Iterator[None]:
    """Time a block: `with stage(st, 'rag'): ...` -> st['rag'] = ms."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        stages[name] = stages.get(name, 0.0) + (time.perf_counter() - t0) * 1000.0


def note_stage(conv_id: str, name: str, ms: float) -> None:
    """Stash a stage timing measured BEFORE the turn recorder runs (e.g. STT
    happens in handle_citizen_voice; the turn itself records later)."""
    if not conv_id:
        return
    with _LOCK:
        _PENDING_STAGES.setdefault(conv_id, {})[name] = round(ms, 1)


def record_turn(*, conv_id: str, agent_id: str, channel: str,
                stages: dict[str, float], speak_reply: bool = False,
                lang: str = "", tool_id: str = "", fallback: bool = False,
                citizen_id: str = "") -> dict:
    """Persist one turn's latency event. Merges any pending stages (STT)."""
    _ensure_loaded()   # load file tail BEFORE first in-memory append
    with _LOCK:
        pending = _PENDING_STAGES.pop(conv_id, {})
    merged = {**pending, **{k: round(v, 1) for k, v in stages.items()}}
    ev = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "conv_id": conv_id,
        "agent_id": agent_id,
        "channel": channel,
        "voice": bool(speak_reply),
        "lang": lang or "",
        "tool": tool_id or "",
        "fallback": bool(fallback),
        # last 6 chars only — enough to correlate, no PII
        "citizen": (citizen_id or "")[-6:],
        "stages": merged,
    }
    with _LOCK:
        _RING.append(ev)
    try:
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass   # metrics must never break a conversation turn
    return ev


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _agg(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "avg": 0, "p50": 0, "p95": 0, "max": 0}
    vs = sorted(values)
    return {
        "count": len(vs),
        "avg": round(sum(vs) / len(vs), 1),
        "p50": round(_pct(vs, 0.50), 1),
        "p95": round(_pct(vs, 0.95), 1),
        "max": round(vs[-1], 1),
    }


def _within_window(ev: dict, cutoff_iso: str) -> bool:
    return (ev.get("ts") or "") >= cutoff_iso


_LOADED = False


def _ensure_loaded() -> None:
    global _LOADED
    if not _LOADED:
        _LOADED = True
        load_recent()


def summary(window_minutes: int = 60) -> dict:
    """Aggregate stage timings over the window: overall, per agent, per channel."""
    _ensure_loaded()
    cutoff = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
    with _LOCK:
        evs = [e for e in _RING if _within_window(e, cutoff)]

    def stage_aggs(events: list[dict]) -> dict:
        out = {}
        for s in STAGE_ORDER:
            vals = [e["stages"][s] for e in events
                    if isinstance(e.get("stages"), dict) and s in e["stages"]]
            a = _agg(vals)
            if a["count"]:
                out[s] = a
        return out

    by_agent: dict[str, list[dict]] = {}
    by_channel: dict[str, list[dict]] = {}
    for e in evs:
        by_agent.setdefault(e.get("agent_id") or "?", []).append(e)
        ch = ("voice" if e.get("voice") else (e.get("channel") or "?"))
        by_channel.setdefault(ch, []).append(e)

    slowest = sorted(
        (e for e in evs if (e.get("stages") or {}).get("total")),
        key=lambda e: e["stages"]["total"], reverse=True)[:10]

    return {
        "window_minutes": window_minutes,
        "turns": len(evs),
        "overall": stage_aggs(evs),
        "per_agent": {a: {"turns": len(es), **{"stages": stage_aggs(es)}}
                      for a, es in sorted(by_agent.items())},
        "per_channel": {c: {"turns": len(es), **{"stages": stage_aggs(es)}}
                        for c, es in sorted(by_channel.items())},
        "slowest": slowest,
        "stage_labels": STAGE_LABELS,
        "stage_order": STAGE_ORDER,
    }


def recent(n: int = 50) -> list[dict]:
    _ensure_loaded()
    with _LOCK:
        return list(_RING)[-n:][::-1]
