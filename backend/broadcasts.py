"""Broadcast composer + storage.

Officers compose a broadcast (title, body, target audience). They can
opt to auto-translate to all supported Indian languages via Sarvam-Translate.
After approval (four-eyes principle), the broadcast is fanned out to all
matching citizens via the existing WS `broadcast` frame.

Phase 5: simple in-memory + JSON-backed list. Audience targeting is
limited to "all" or "by pincode". Phase 7 adds richer segmentation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings
from .store import store
from .ws_manager import ws_manager

log = logging.getLogger("broadcasts")


@dataclass
class Broadcast:
    broadcast_id: str
    agent_id: str
    title: str
    body: str
    target_audience: dict           # {"type": "all"} | {"type": "pincodes", "pincodes": [...]}
    languages: list[str]            # ISO codes; empty = single-language
    bodies_by_language: dict[str, str] = field(default_factory=dict)
    composed_by: str = "officer-1"
    composed_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "draft"           # draft | pending_approval | approved | sent | rejected
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    sent_count: int = 0


_BCASTS: dict[str, Broadcast] = {}


def _bcast_path() -> Path:
    return Path(settings.data_dir) / "broadcasts.json"


def _persist() -> None:
    try:
        payload = {
            bid: {
                **{k: v for k, v in b.__dict__.items()
                   if not isinstance(v, datetime)},
                "composed_at": b.composed_at.isoformat() if b.composed_at else None,
                "approved_at": b.approved_at.isoformat() if b.approved_at else None,
            }
            for bid, b in _BCASTS.items()
        }
        _bcast_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    except Exception as e:
        log.warning("Broadcast persist failed: %s", e)


def _load() -> None:
    p = _bcast_path()
    if not p.exists():
        return
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        for bid, d in payload.items():
            for tf in ("composed_at", "approved_at"):
                if d.get(tf):
                    try:
                        d[tf] = datetime.fromisoformat(d[tf])
                    except Exception:
                        d[tf] = None
            try:
                _BCASTS[bid] = Broadcast(**d)
            except Exception:
                pass
    except Exception as e:
        log.warning("Broadcast load failed: %s", e)


_load()


def create(*, agent_id: str, title: str, body: str,
           target_audience: dict, languages: list[str],
           composed_by: str = "officer-1") -> Broadcast:
    bid = f"bcast_{uuid.uuid4().hex[:10]}"
    b = Broadcast(
        broadcast_id=bid, agent_id=agent_id, title=title, body=body,
        target_audience=target_audience or {"type": "all"},
        languages=languages or ["en-IN"],
        bodies_by_language={"en-IN": body} if not languages else {},
        composed_by=composed_by, status="pending_approval",
    )
    _BCASTS[bid] = b
    _persist()
    return b


async def translate_into(bid: str, languages: list[str]) -> Broadcast:
    """Use Sarvam-Translate (real) or stub (mock) to fill bodies_by_language."""
    b = _BCASTS.get(bid)
    if not b:
        raise KeyError(bid)
    # If LLM is mock-mode, we don't actually translate — we just label.
    if settings.mock_mode:
        for lang in languages:
            b.bodies_by_language[lang] = f"[{lang}] {b.body}"
    else:
        # Real Sarvam-Translate call — uses the chat LLM endpoint to
        # avoid a new dependency. For Phase 7 we'd use the dedicated
        # /text/translate endpoint.
        from .llm import llm
        for lang in languages:
            if lang in b.bodies_by_language:
                continue
            msgs = [
                {"role": "system",
                 "content": "You are a translator. Translate the user's text "
                            f"into {lang}. Respond with ONLY the translation, no prose."},
                {"role": "user", "content": b.body},
            ]
            try:
                t = await llm.chat_complete(messages=msgs, temperature=0.1, max_tokens=600)
                b.bodies_by_language[lang] = t.strip()
            except Exception as e:
                log.warning("Translation to %s failed: %s", lang, e)
                b.bodies_by_language[lang] = f"[{lang}] {b.body}"
    _persist()
    return b


def approve(bid: str, approved_by: str) -> Optional[Broadcast]:
    b = _BCASTS.get(bid)
    if not b:
        return None
    if b.composed_by == approved_by:
        log.warning("Four-eyes violation: %s tried to approve their own broadcast", approved_by)
        return None
    b.status = "approved"
    b.approved_by = approved_by
    b.approved_at = datetime.utcnow()
    _persist()
    return b


def reject(bid: str, by: str) -> Optional[Broadcast]:
    b = _BCASTS.get(bid)
    if not b:
        return None
    b.status = "rejected"
    b.approved_by = by
    b.approved_at = datetime.utcnow()
    _persist()
    return b


async def send(bid: str) -> int:
    """Fan out to all connected citizens (Phase 5 = all; Phase 7 = segmented)."""
    b = _BCASTS.get(bid)
    if not b or b.status != "approved":
        return 0
    # For each connected citizen, pick the body in their preferred language
    # (fall back to English).
    all_citizens = list(store.citizens.keys())
    n = 0
    for cid in all_citizens:
        citizen = store.get_citizen(cid) or {}
        lang = citizen.get("language", "en-IN")
        body = (b.bodies_by_language.get(lang)
                or b.bodies_by_language.get("en-IN")
                or b.body)
        await ws_manager.send_to_citizen(cid, {
            "type": "broadcast",
            "broadcastId": b.broadcast_id,
            "agentId": b.agent_id,
            "title": b.title, "body": body,
        })
        n += 1
    b.status = "sent"
    b.sent_count = n
    _persist()
    return n


def list_all() -> list[Broadcast]:
    return sorted(_BCASTS.values(), key=lambda b: b.composed_at, reverse=True)


def get(bid: str) -> Optional[Broadcast]:
    return _BCASTS.get(bid)
