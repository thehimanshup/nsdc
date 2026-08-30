"""Operator-controlled tool bindings — enable/disable + agent wiring.

This is the *only* state the Tools page edits. It is deliberately kept
separate from code so the two concerns never collide:

  - WHAT a tool is and does (metadata + execute)  -> Python (tools.py / plugins / MCP)
  - WHETHER it's on, and which agents use it       -> data/tool_bindings.json (this module)

So a developer redeploying code never wipes the operator's wiring, and an
operator flipping switches never touches code.

File schema (keyed by tool id)::

    {
      "revenue.check_dues":  { "enabled": true,  "agents": ["revenue"] },
      "digilocker.fetch_dl": { "enabled": false, "agents": ["transport", "cmo"] }
    }

`tools_for_agent()` reads `get()` fresh on every turn, so an edit here takes
effect on the next chat message — no restart. Same atomic-write + in-memory
cache style as `admin_storage.py`.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import RLock

from .config import settings

log = logging.getLogger("tools.bindings")
_LOCK = RLock()
_CACHE: dict[str, dict] = {}
_LOADED = False


def _path() -> Path:
    p = Path(settings.data_dir) / "tool_bindings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _normalise(raw: dict) -> dict[str, dict]:
    """Coerce a parsed file into {tool_id: {enabled: bool, agents: [str]}}."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for tid, b in raw.items():
        if not isinstance(b, dict):
            continue
        out[tid] = {
            "enabled": bool(b.get("enabled", True)),
            "agents": [str(a) for a in (b.get("agents") or []) if a],
        }
    return out


def load() -> int:
    """Read the bindings file into the in-memory cache. Returns the count.

    A missing file is fine (no bindings yet -> tools fall back to their
    in-code default_agents). A corrupt file is logged and treated as empty
    so a bad edit can never take tools offline silently at startup."""
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
    log.info("Loaded %d tool bindings from %s", len(_CACHE), path)
    return len(_CACHE)


def _ensure_loaded() -> None:
    if not _LOADED:
        try:
            load()
        except Exception:  # noqa: BLE001 — never let a binding read break a turn
            pass


def get(tool_id: str) -> dict | None:
    """Return the binding for a tool, or None if it has none (caller then
    falls back to the tool's in-code allowed_agents)."""
    _ensure_loaded()
    with _LOCK:
        b = _CACHE.get(tool_id)
        return dict(b) if b is not None else None


def all_bindings() -> dict[str, dict]:
    _ensure_loaded()
    with _LOCK:
        return {k: dict(v) for k, v in _CACHE.items()}


def _save_locked() -> None:
    """Atomic write of the current cache. Caller must hold _LOCK."""
    path = _path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_CACHE, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def set_binding(tool_id: str, *, enabled: bool, agents: list[str]) -> dict:
    """Insert/update one binding and persist. Returns the saved binding."""
    _ensure_loaded()
    with _LOCK:
        b = {"enabled": bool(enabled),
             "agents": [str(a) for a in (agents or []) if a]}
        _CACHE[tool_id] = b
        _save_locked()
    log.info("Saved tool binding: %s -> %s", tool_id, b)
    return dict(b)


def delete_binding(tool_id: str) -> bool:
    """Remove a binding (tool reverts to its in-code default_agents)."""
    _ensure_loaded()
    with _LOCK:
        if tool_id not in _CACHE:
            return False
        del _CACHE[tool_id]
        _save_locked()
    log.info("Deleted tool binding: %s", tool_id)
    return True
