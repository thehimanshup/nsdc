"""Operator-controlled skill bindings — enable/disable + agent wiring.

The skills analogue of `tool_bindings.py`. WHAT a skill is (tools, instructions,
corpus) lives in data/skills/*.json; WHETHER it's on and which agents get it
lives here, so a redeploy never wipes the operator's wiring.

File schema (keyed by skill id)::

    {
      "land_dispute": { "enabled": true,  "agents": ["revenue", "cmo"] },
      "pension_help": { "enabled": false, "agents": ["social"] }
    }

`skills_for_agent()` reads `get()` fresh on every turn, so an edit takes effect
on the next chat message — no restart.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import RLock

from .config import settings

log = logging.getLogger("skills.bindings")
_LOCK = RLock()
_CACHE: dict[str, dict] = {}
_LOADED = False


def _path() -> Path:
    p = Path(settings.data_dir) / "skill_bindings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _normalise(raw: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for sid, b in raw.items():
        if not isinstance(b, dict):
            continue
        out[sid] = {
            "enabled": bool(b.get("enabled", True)),
            "agents": [str(a) for a in (b.get("agents") or []) if a],
        }
    return out


def load() -> int:
    """Read the bindings file into the in-memory cache. Returns the count.

    A missing file is fine (skills fall back to their default_agents). A corrupt
    file is logged and treated as empty so a bad edit can't silently break."""
    global _LOADED
    path = _path()
    with _LOCK:
        if not path.exists():
            _CACHE.clear()
            _LOADED = True
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception as e:
            log.error("Failed to read %s: %s — treating as empty", path, e)
            raw = {}
        _CACHE.clear()
        _CACHE.update(_normalise(raw))
        _LOADED = True
    log.info("Loaded %d skill bindings from %s", len(_CACHE), path)
    return len(_CACHE)


def _ensure_loaded() -> None:
    if not _LOADED:
        try:
            load()
        except Exception:  # noqa: BLE001 — never let a binding read break a turn
            pass


def get(skill_id: str) -> dict | None:
    _ensure_loaded()
    with _LOCK:
        b = _CACHE.get(skill_id)
        return dict(b) if b is not None else None


def all_bindings() -> dict[str, dict]:
    _ensure_loaded()
    with _LOCK:
        return {k: dict(v) for k, v in _CACHE.items()}


def _save_locked() -> None:
    path = _path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_CACHE, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def set_binding(skill_id: str, *, enabled: bool, agents: list[str]) -> dict:
    """Insert/update one binding and persist. Returns the saved binding."""
    _ensure_loaded()
    with _LOCK:
        b = {"enabled": bool(enabled),
             "agents": [str(a) for a in (agents or []) if a]}
        _CACHE[skill_id] = b
        _save_locked()
    log.info("Saved skill binding: %s -> %s", skill_id, b)
    return dict(b)


def delete_binding(skill_id: str) -> bool:
    """Remove a binding (skill reverts to its default_agents)."""
    _ensure_loaded()
    with _LOCK:
        if skill_id not in _CACHE:
            return False
        del _CACHE[skill_id]
        _save_locked()
    log.info("Deleted skill binding: %s", skill_id)
    return True
