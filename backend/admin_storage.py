"""Dynamic agent registry — JSON-backed, hot-reloadable.

Phase 1-4: agents were defined in code in agents.py.
Phase 5: agents are loaded from data/agents.json on startup, with the
in-code defaults as the seed. The admin console CRUDs entries in the
JSON file and the registry hot-reloads.

The legacy `AGENTS` dict in agents.py is preserved as the seed source.
After the JSON file exists, edits there are the source of truth.

Schema (per agent):
    id, name, emoji, color, bg, description, pinned, voice,
    department_block, mock_responses[], push_pool[],
    tool_ids[], corpus_id, llm_provider (optional, overrides global)
"""
from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
from threading import RLock
from typing import Optional

from . import agents as _legacy_agents
from .agents import Agent
from .config import settings

log = logging.getLogger("admin.storage")
_LOCK = RLock()


def _storage_path() -> Path:
    p = Path(settings.data_dir) / "agents.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _serialise_agent(a: Agent) -> dict:
    """Agent -> JSON-friendly dict."""
    d = {fld.name: getattr(a, fld.name) for fld in fields(Agent)}
    # Strip unknown fields if any
    return d


def _deserialise_agent(d: dict) -> Agent:
    """JSON-friendly dict -> Agent."""
    known = {f.name for f in fields(Agent)}
    safe = {k: v for k, v in d.items() if k in known}
    # Add llm_provider via setattr after construction since the base Agent
    # dataclass may not have it; we store it in extra on the dataclass instead.
    return Agent(**safe)


def _seed_from_legacy() -> None:
    """If agents.json doesn't exist (or is empty/corrupt), populate it
    from the in-code defaults. The empty-file check is needed in mounted
    filesystems where a reset truncates to 0 bytes rather than unlinking."""
    path = _storage_path()
    needs_seed = False
    if not path.exists():
        needs_seed = True
    else:
        try:
            txt = path.read_text(encoding="utf-8").strip()
            if not txt:
                needs_seed = True
            else:
                d = json.loads(txt)
                if not isinstance(d, dict) or not d:
                    needs_seed = True
        except Exception:
            needs_seed = True
    if not needs_seed:
        return
    log.info("Seeding agents.json from in-code defaults")
    payload = {a.id: _serialise_agent(a) for a in _legacy_agents.all_agents()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def _migrate_voices(payload: dict) -> bool:
    """Rewrite any v2-only or fabricated voices in agents.json to valid v3
    voices so Bulbul never receives an unknown speaker.

    Returns True if the file should be re-saved (something changed).
    """
    # Same remap table as backend/voice.py — kept here to avoid an import cycle
    LEGACY = {
        "manisha": "simran", "vidya": "priya", "anushka": "ritu",
        "arya": "kavya", "abhilash": "rahul", "karun": "rohan",
        "hitesh": "shubh", "arjun": "rahul", "anjali": "ritu",
        "amol": "amit", "diya": "neha",
    }
    VALID = {
        "shubh", "aditya", "rahul", "rohan", "varun", "amit", "dev", "kabir",
        "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "vijay",
        "mohit", "rehan", "soham", "aayan", "manan", "sumit", "gokul",
        "ratan", "ritu", "priya", "neha", "pooja", "simran", "kavya",
        "ishita", "shreya", "roopa", "tanya", "shruti", "suhani", "kavitha",
        "rupali",
    }
    changed = False
    for aid, d in payload.items():
        if not isinstance(d, dict):
            continue
        v = d.get("voice")
        if not v or v in VALID:
            continue
        new_v = LEGACY.get(v, "shubh")
        d["voice"] = new_v
        log.warning("Migrated agent %s voice: %s → %s (was not a valid bulbul:v3 voice)",
                    aid, v, new_v)
        changed = True
    return changed


def _backfill_builtin_personas(payload: dict) -> bool:
    """Populate new persona fields on existing built-in agents."""
    changed = False
    for aid, d in payload.items():
        if not isinstance(d, dict):
            continue
        default = _legacy_agents.AGENTS.get(aid)
        if not default:
            continue
        for key in ("persona_variants", "voice_pool"):
            if not d.get(key):
                value = getattr(default, key, None)
                if value:
                    d[key] = deepcopy(value)
                    changed = True
    return changed


def load_into_registry() -> int:
    """Read agents.json and load into the live agents.AGENTS dict.
    Returns the count loaded. Logs warnings for any malformed entries.

    Phase 5b: auto-migrates legacy / invalid voice names so callers never
    end up hitting Bulbul with an unknown speaker (which produces a silent
    chime fallback that's confusing to debug).
    """
    _seed_from_legacy()
    path = _storage_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Failed to read %s: %s — keeping in-code defaults", path, e)
        return 0
    if not isinstance(payload, dict):
        log.error("agents.json must be an object keyed by agent id; got %s",
                  type(payload).__name__)
        return 0

    # Migrate any stale voices (e.g. file written before Phase 5b voice fix)
    changed = False
    if _migrate_voices(payload):
        changed = True
    if _backfill_builtin_personas(payload):
        changed = True
    if changed:
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
            log.info("Saved migrated agents.json with built-in persona/voice backfills")
        except Exception as e:
            log.warning("Could not save migrated agents.json: %s", e)

    with _LOCK:
        _legacy_agents.AGENTS.clear()
        for aid, d in payload.items():
            try:
                a = _deserialise_agent(d)
                _legacy_agents.AGENTS[a.id] = a
            except Exception as e:
                log.warning("Skipping agent %s: %s", aid, e)
    log.info("Loaded %d agents from %s", len(_legacy_agents.AGENTS), path)
    return len(_legacy_agents.AGENTS)


def save_agent(a: Agent) -> None:
    """Persist a single agent (insert or update)."""
    path = _storage_path()
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            payload = {}
        payload[a.id] = _serialise_agent(a)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        _legacy_agents.AGENTS[a.id] = a
    log.info("Saved agent: %s", a.id)


def delete_agent(agent_id: str) -> bool:
    """Remove an agent from storage + registry."""
    path = _storage_path()
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if agent_id not in payload:
            return False
        del payload[agent_id]
        _legacy_agents.AGENTS.pop(agent_id, None)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    log.info("Deleted agent: %s", agent_id)
    return True


def reset_to_defaults() -> int:
    """Wipe agents.json and re-seed from in-code defaults. Useful for demos."""
    path = _storage_path()
    if path.exists():
        path.unlink()
    # Re-import the legacy agents module to re-populate the dict
    import importlib
    importlib.reload(_legacy_agents)
    _seed_from_legacy()
    return load_into_registry()
