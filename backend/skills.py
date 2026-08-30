"""Skills — attachable bundles of tools + instructions (Phase 7, Milestone 3).

A *skill* packages a capability an agent can be granted as a unit:

  - `tool_ids`     : existing registry tools (builtin / plugin / mcp.*) the
                     skill brings to the agent;
  - `instructions` : a system-prompt fragment injected when the skill is
                     attached (procedural know-how for using those tools);
  - `corpus_id`    : an optional RAG corpus added to retrieval for the turn.

Skills are *data*, not code — each one is a JSON file under `data/skills/`, so
an operator can add an external skill (e.g. one that bundles an MCP server's
tools + instructions) without a deploy. WHICH agents get a skill is operator
wiring in `data/skill_bindings.json` (see `skill_bindings.py`), exactly like
`tool_bindings.json` governs tools.

This mirrors the failure-isolation of the tool/MCP loaders: a missing folder,
a corrupt file, or one bad skill is logged and skipped — it never breaks
startup or the other skills.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

log = logging.getLogger("skills")


@dataclass
class Skill:
    id: str
    name: str
    description: str
    instructions: str = ""
    tool_ids: list[str] = field(default_factory=list)
    corpus_id: str = ""
    # Suggested wiring; the live wiring lives in data/skill_bindings.json. With
    # no binding for a skill, `skills_for_agent()` falls back to this list
    # (parallel to a tool's allowed_agents).
    default_agents: list[str] = field(default_factory=list)
    source: str = "builtin"   # builtin | plugin | external
    enabled: bool = True       # developer master switch (operator override in binding)
    # Outbound callback contract (Callback Agent Platform). None = an ordinary
    # inbound skill (current behaviour). When present, it's the declarative
    # `steps[]` state machine the outbound engine interprets — see
    # `outbound_contract.py` for the grammar + validator and
    # docs/callback-agent-platform-plan.md for the design.
    outbound: dict | None = None


_SKILLS: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    _SKILLS[skill.id] = skill


def get_skill(skill_id: str) -> Skill | None:
    return _SKILLS.get(skill_id)


def all_skills() -> list[Skill]:
    return list(_SKILLS.values())


# ---------------------------------------------------------------------------
# Loader — data/skills/*.json (one skill per file)
# ---------------------------------------------------------------------------

def _dir() -> Path:
    p = Path(settings.data_dir) / "skills"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _coerce(raw: dict, *, fallback_id: str) -> Skill | None:
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("id") or fallback_id).strip()
    if not sid:
        return None
    return Skill(
        id=sid,
        name=str(raw.get("name") or sid),
        description=str(raw.get("description") or ""),
        instructions=str(raw.get("instructions") or ""),
        tool_ids=[str(t) for t in (raw.get("tool_ids") or []) if t],
        corpus_id=str(raw.get("corpus_id") or ""),
        default_agents=[str(a) for a in (raw.get("default_agents") or []) if a],
        source=str(raw.get("source") or "external"),
        enabled=bool(raw.get("enabled", True)),
        outbound=raw.get("outbound") if isinstance(raw.get("outbound"), dict) else None,
    )


def parse_skill_md(text: str) -> dict:
    """Parse a SKILL.md (YAML frontmatter + markdown body) into a skill dict.

    Format::

        ---
        id: my_skill
        name: My Skill
        tool_ids: [tool.a, tool.b]
        ---
        <the markdown body becomes the skill's `instructions`>

    The frontmatter carries the metadata; the body after the closing `---`
    becomes `instructions` (unless the frontmatter sets `instructions`
    explicitly). Raises ValueError on malformed frontmatter."""
    import yaml
    text = (text or "").lstrip("﻿")           # strip BOM
    fm_text, body = "", text
    stripped = text.lstrip()
    if stripped.startswith("---"):
        rest = stripped[3:]
        end = rest.find("\n---")
        if end != -1:
            fm_text = rest[:end]
            after = rest[end + 4:]                  # skip the closing '---'
            nl = after.find("\n")
            body = after[nl + 1:] if nl != -1 else ""
    try:
        fm = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid YAML frontmatter: {e}")
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    out = dict(fm)
    if not str(out.get("instructions") or "").strip():
        out["instructions"] = body.strip()
    return out


def load() -> int:
    """(Re)load every skill under data/skills/ — both ``*.json`` and SKILL-style
    ``*.md`` (YAML frontmatter + body). Returns the skill count.

    A missing folder is fine (no skills). One bad file is logged and skipped.
    If the same id exists as both .json and .md, the .md wins (loaded last)."""
    folder = _dir()
    _SKILLS.clear()
    if not folder.exists():
        return 0
    for fp in sorted(folder.glob("*.json")) + sorted(folder.glob("*.md")):
        try:
            if fp.suffix.lower() == ".md":
                raw = parse_skill_md(fp.read_text(encoding="utf-8"))
            else:
                raw = json.loads(fp.read_text(encoding="utf-8") or "{}")
        except Exception as e:  # noqa: BLE001 — one bad skill must not kill the rest
            log.error("Skipping skill file %s: %s", fp.name, e)
            continue
        sk = _coerce(raw, fallback_id=fp.stem)
        if sk is None:
            log.warning("Skipping skill file %s: not a valid skill object", fp.name)
            continue
        if sk.outbound is not None:
            def validate_outbound(spec):
                return []  # outbound platform pruned in nsdc-substrate-poc
            errs = validate_outbound(sk.outbound)
            if errs:
                log.warning("Skipping skill file %s: invalid outbound contract: %s",
                            fp.name, "; ".join(errs))
                continue
        register_skill(sk)
    log.info("Loaded %d skill(s) from %s", len(_SKILLS), folder)
    return len(_SKILLS)


# ---------------------------------------------------------------------------
# External-skill management — add/edit/delete a skill JSON from the admin UI
# ---------------------------------------------------------------------------

def save_skill(*, id: str, name: str, description: str = "",
               instructions: str = "", tool_ids: list[str] | None = None,
               corpus_id: str = "", default_agents: list[str] | None = None,
               source: str = "external", enabled: bool = True,
               outbound: dict | None = None) -> Skill:
    """Write one skill to data/skills/<id>.json (atomic) and refresh the
    registry. Caller is responsible for validating `id` is filename-safe.

    Raises ValueError if `outbound` is present but not a valid callback
    contract, so the admin endpoint can surface the errors to the operator."""
    if outbound is not None:
        def validate_outbound(spec):
            return []  # outbound platform pruned in nsdc-substrate-poc
        errs = validate_outbound(outbound)
        if errs:
            raise ValueError("Invalid outbound contract: " + "; ".join(errs))
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "id": id, "name": name or id, "description": description,
        "instructions": instructions,
        "tool_ids": [str(t) for t in (tool_ids or []) if t],
        "corpus_id": corpus_id,
        "default_agents": [str(a) for a in (default_agents or []) if a],
        "source": source, "enabled": bool(enabled),
    }
    if outbound is not None:
        data["outbound"] = outbound
    path = folder / f"{id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    # avoid the same id being defined in two formats (.json + .md)
    md = folder / f"{id}.md"
    if md.exists():
        md.unlink()
    load()  # re-read the folder so the registry reflects the change
    log.info("Saved skill: %s (%d tool(s))", id, len(data["tool_ids"]))
    return get_skill(id)  # type: ignore[return-value]


def save_skill_md(md_text: str) -> Skill:
    """Persist a SKILL.md (frontmatter + body) as data/skills/<id>.md and refresh.

    The id comes from the frontmatter. Any competing <id>.json is removed so the
    skill isn't defined twice. Raises ValueError on malformed md / bad outbound."""
    raw = parse_skill_md(md_text)
    sid = str(raw.get("id") or "").strip()
    if not sid:
        raise ValueError("SKILL.md frontmatter must include an 'id'")
    if isinstance(raw.get("outbound"), dict):
        def validate_outbound(spec):
            return []  # outbound platform pruned in nsdc-substrate-poc
        errs = validate_outbound(raw["outbound"])
        if errs:
            raise ValueError("Invalid outbound contract: " + "; ".join(errs))
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(md_text, encoding="utf-8")
    os.replace(tmp, path)
    jp = folder / f"{sid}.json"          # drop a competing JSON of the same id
    if jp.exists():
        jp.unlink()
    load()
    log.info("Saved SKILL.md: %s", sid)
    return get_skill(sid)  # type: ignore[return-value]


def delete_skill(skill_id: str) -> bool:
    """Delete a skill's file (.json or .md) and drop it from the registry."""
    folder = _dir()
    jp, mp = folder / f"{skill_id}.json", folder / f"{skill_id}.md"
    existed = jp.exists() or mp.exists() or skill_id in _SKILLS
    for p in (jp, mp):
        if p.exists():
            p.unlink()
    _SKILLS.pop(skill_id, None)
    log.info("Deleted skill: %s", skill_id)
    return existed


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

def skills_for_agent(agent_id: str) -> list[Skill]:
    """Skills attached to an agent this turn (operator wiring applies).

    Mirrors `tools.tools_for_agent`: the binding in data/skill_bindings.json
    wins; with no binding for a skill, fall back to its `default_agents`. A
    skill disabled in code (enabled=False) or in its binding is never attached.
    Returns [] when skills are globally disabled (SKILLS_ENABLED=false).
    """
    if not settings.skills_enabled:
        return []
    from . import skill_bindings
    out: list[Skill] = []
    for s in _SKILLS.values():
        if not s.enabled:
            continue
        b = skill_bindings.get(s.id)
        if b is not None and not b.get("enabled", True):
            continue
        agents = b.get("agents") if b is not None else s.default_agents
        if agent_id in (agents or []):
            out.append(s)
    return out
