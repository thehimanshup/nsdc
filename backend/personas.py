"""Per-agent few-shot example library + persona helpers.

Phase 6c upgrades the agent system prompt with:

  - A few-shot block of "User → You" exchanges so the LLM has a concrete
    pattern to imitate.
  - A tone profile (length budget + style hints) calibrated per channel.

Each agent ships with 6-10 hand-written example pairs in the voice of
its persona (Senthil, Aravind, Dr. Lakshmi, Karthik, Manikandan, Devi,
Priya).  Examples live in `data/personas/{agent_id}_examples.jsonl`
and can be edited live by admin without redeploy.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from threading import RLock
from typing import Optional

from .config import settings
from .models import Channel
from .retrieval.bm25 import tokenize

log = logging.getLogger("personas")

_LOCK = RLock()
_EXAMPLES_CACHE: dict[str, list[dict]] = {}
# Phase 6f — voice-call-specific examples (short, spoken, no lists/URLs).
# Stored in data/personas/voice/{agent}_examples.jsonl so they never collide
# with the chat example glob. The voice/live-call path uses these so every
# agent mimics a natural human-on-the-phone style instead of WhatsApp prose.
_VOICE_CACHE: dict[str, list[dict]] = {}

_LANGUAGE_FAMILIES: dict[str, str] = {
    "en-IN": "latin",
    "hi-IN": "devanagari",
    "mr-IN": "devanagari",
    "ne-IN": "devanagari",
    "sa-IN": "devanagari",
    "kok-IN": "devanagari",
    "bn-IN": "bengali",
    "as-IN": "bengali",
    "pa-IN": "gurmukhi",
    "gu-IN": "gujarati",
    "od-IN": "odia",
    "ta-IN": "tamil",
    "te-IN": "telugu",
    "kn-IN": "kannada",
    "ml-IN": "malayalam",
    "ur-IN": "arabic",
    "ks-IN": "arabic",
}


def _language_family(lang: str) -> str:
    return _LANGUAGE_FAMILIES.get((lang or "").strip(), (lang or "latin"))


# ---------------------------------------------------------------------------
# Channel-specific tone & length budgets
# ---------------------------------------------------------------------------

CHANNEL_PROFILES: dict[str, dict] = {
    "simulator": {
        # Phase 6g — validation feedback: chat answers were "too concise" and
        # never linked anywhere. Chat may now be substantive (greetings stay
        # short) and SHOULD link the official portal/page when one exists.
        "max_sentences": 8,
        "style_hint": (
            "App chat — match depth to the question. For a greeting ('hi'), reply with "
            "ONE greeting sentence plus ONE offer-of-help sentence, nothing more. For a "
            "substantive question, give a COMPLETE, helpful answer: 3-8 short sentences, "
            "and a short '-' bullet list when listing documents, steps, or options. "
            "When an official portal, scheme page, or form has a URL you know from the "
            "provided context, INCLUDE it as a markdown link like "
            "[TN eSevai](https://www.tnesevai.tn.gov.in) — the app renders links as "
            "clickable. Also give the relevant helpline number. Never pad with filler; "
            "end with at most one follow-up question."),
        "allow_lists": True,
    },
    "twilio_wa": {
        "max_sentences": 6,
        "style_hint": (
            "WhatsApp Business — match depth to the question: ONE greeting + ONE offer "
            "line for greetings; for real questions, 3-6 short sentences with the key "
            "facts, the helpline, and the official URL written plainly (WhatsApp "
            "auto-links bare URLs; do NOT use markdown link syntax). Short '-' lists "
            "are fine for documents/steps."),
        "allow_lists": True,
    },
    "voice": {
        "max_sentences": 3,
        "style_hint": (
            "VOICE CALL — you are a real human officer on a live phone call. Sound like "
            "a warm, competent person, NOT a website being read aloud.\n"
            "  HOW A HUMAN TALKS ON THE PHONE:\n"
            "  - Open by ACKNOWLEDGING what the caller just said in your own words "
            "(e.g. 'Oh no, three days with no water — that's hard', 'Got it', "
            "'I understand'), THEN help. Lead with empathy when they're worried.\n"
            "  - Use natural spoken contractions (I'll, you'll, let's, that's, don't) and "
            "everyday phrasing. Vary how you start each turn — never robotic.\n"
            "  - Say ONE thing at a time. 1-3 short sentences, then STOP and let them talk. "
            "If there are several steps, give the FIRST step and offer the next — "
            "do NOT recite a numbered checklist.\n"
            "  - Ask AT MOST one question per turn, and only if you truly need it.\n"
            "  FORBIDDEN ON A CALL: numbered or bulleted lists ('one… two… three…'), "
            "markdown, reading out URLs/web addresses, spelling long IDs digit-by-digit "
            "unasked, citations, or saying 'as an AI/assistant'. Give a website only if "
            "asked, and say it as plain words.\n"
            "  ANTI-REPEAT: never re-introduce yourself after the greeting, never restate "
            "your previous sentence, and don't repeat a helpline or reference number you "
            "already gave unless the caller asks again.\n"
            "  If the caller interrupts, stop and listen."),
        "allow_lists": False,
    },
    # Live-call channels resolve to the same human-call profile as "voice".
    "livekit_app": {
        "max_sentences": 3,
        "style_hint": "alias of voice — see voice profile",
        "allow_lists": False,
        "_alias": "voice",
    },
    "twilio_voice": {
        "max_sentences": 3,
        "style_hint": "alias of voice — see voice profile",
        "allow_lists": False,
        "_alias": "voice",
    },
    "system": {
        "max_sentences": 4,
        "style_hint": "Friendly, concise, 1-4 short sentences. No numbered analysis.",
        "allow_lists": True,
    },
}


def channel_tone_block(channel: str) -> str:
    """Render the per-channel style guidance that goes into the system prompt."""
    profile = CHANNEL_PROFILES.get(channel) or CHANNEL_PROFILES["system"]
    alias = profile.get("_alias")
    if alias:
        profile = CHANNEL_PROFILES.get(alias, profile)
    return (
        f"CHANNEL: {channel}\n"
        f"  - max ≈ {profile['max_sentences']} sentences\n"
        f"  - {profile['style_hint']}"
    )


# ---------------------------------------------------------------------------
# Few-shot library
# ---------------------------------------------------------------------------

def _examples_path(agent_id: str) -> Path:
    p = Path(settings.data_dir) / "personas"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{agent_id}_examples.jsonl"


def load_examples() -> int:
    """Scan data/personas/*.jsonl and cache them. Returns total examples."""
    base = Path(settings.data_dir) / "personas"
    if not base.exists():
        return 0
    total = 0
    with _LOCK:
        _EXAMPLES_CACHE.clear()
        for f in sorted(base.glob("*_examples.jsonl")):
            agent_id = f.name.replace("_examples.jsonl", "")
            rows: list[dict] = []
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception as e:
                    log.warning("bad example in %s: %s", f, e)
            _EXAMPLES_CACHE[agent_id] = rows
            total += len(rows)
            log.info("Loaded %d few-shot examples for agent %s", len(rows), agent_id)
    return total


def load_voice_examples() -> int:
    """Scan data/personas/voice/*_examples.jsonl and cache them.

    These are the short, spoken-style exchanges the live-call/voice path uses
    so each agent sounds like a real human officer on the phone. Returns the
    total number of voice examples loaded.
    """
    base = Path(settings.data_dir) / "personas" / "voice"
    total = 0
    with _LOCK:
        _VOICE_CACHE.clear()
        if not base.exists():
            return 0
        for f in sorted(base.glob("*_examples.jsonl")):
            agent_id = f.name.replace("_examples.jsonl", "")
            rows: list[dict] = []
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception as e:
                    log.warning("bad voice example in %s: %s", f, e)
            _VOICE_CACHE[agent_id] = rows
            total += len(rows)
            log.info("Loaded %d VOICE examples for agent %s", len(rows), agent_id)
    return total


def pick_examples(agent_id: str, query: str, *, n: int = 3,
                  voice: bool = False, lang: str = "") -> list[dict]:
    """Pick the top-N most query-relevant examples by simple token overlap.

    When ``voice=True`` the voice-specific bank is preferred; if an agent has
    no voice examples yet we fall back to its chat examples so behaviour never
    regresses.
    """
    with _LOCK:
        if voice:
            pool = list(_VOICE_CACHE.get(agent_id, [])) or list(
                _EXAMPLES_CACHE.get(agent_id, []))
        else:
            pool = list(_EXAMPLES_CACHE.get(agent_id, []))
    if not pool:
        return []
    if lang:
        exact = [ex for ex in pool if (ex.get("language") or "").strip() == lang]
        family = [ex for ex in pool
                  if _language_family(ex.get("language") or "") == _language_family(lang)]
        if exact:
            pool = exact
        elif family:
            pool = family
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return random.sample(pool, min(n, len(pool)))

    def overlap_score(ex: dict) -> float:
        u_tokens = set(tokenize(ex.get("user", "")))
        if not u_tokens:
            return 0.0
        common = len(q_tokens & u_tokens)
        return common / (len(u_tokens) ** 0.5)
    scored = sorted(pool, key=overlap_score, reverse=True)
    # If top scores are all 0, just return random samples
    top = scored[:n]
    if all(overlap_score(t) < 0.001 for t in top):
        return random.sample(pool, min(n, len(pool)))
    return top


def render_few_shot_block(agent_id: str, query: str, *, n: int = 3,
                          voice: bool = False, lang: str = "") -> str:
    """Render the few-shot exchanges as a string ready to inject into the system prompt.

    When ``voice=True`` the spoken-style voice example bank is used so the
    live-call agent imitates a natural phone manner.
    """
    picks = pick_examples(agent_id, query, n=n, voice=voice, lang=lang)
    if not picks:
        return ""
    lines: list[str] = []
    for ex in picks:
        u = (ex.get("user") or "").strip()
        a = (ex.get("agent") or "").strip()
        if not u or not a:
            continue
        lines.append(f'CITIZEN: "{u}"')
        lines.append(f"YOU: \"{a}\"")
        lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence — admin can edit examples from the UI
# ---------------------------------------------------------------------------

def list_examples(agent_id: str) -> list[dict]:
    with _LOCK:
        return list(_EXAMPLES_CACHE.get(agent_id, []))


def add_example(agent_id: str, user_text: str, agent_text: str,
                 *, language: str = "en-IN", tags: Optional[list[str]] = None) -> dict:
    ex = {
        "user": user_text.strip(),
        "agent": agent_text.strip(),
        "language": language,
        "tags": tags or [],
    }
    with _LOCK:
        _EXAMPLES_CACHE.setdefault(agent_id, []).append(ex)
        with open(_examples_path(agent_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    log.info("Added example for agent %s (now %d total)",
             agent_id, len(_EXAMPLES_CACHE[agent_id]))
    return ex


def delete_example(agent_id: str, idx: int) -> bool:
    with _LOCK:
        pool = _EXAMPLES_CACHE.get(agent_id, [])
        if idx < 0 or idx >= len(pool):
            return False
        pool.pop(idx)
        # Rewrite file
        path = _examples_path(agent_id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for ex in pool:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return True


def replace_all(agent_id: str, examples: list[dict]) -> int:
    """Overwrite the example file for an agent."""
    with _LOCK:
        _EXAMPLES_CACHE[agent_id] = list(examples)
        path = _examples_path(agent_id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return len(examples)


def stats() -> dict[str, int]:
    with _LOCK:
        return {k: len(v) for k, v in _EXAMPLES_CACHE.items()}


def voice_stats() -> dict[str, int]:
    """Per-agent count of spoken-style voice examples (Phase 6f)."""
    with _LOCK:
        return {k: len(v) for k, v in _VOICE_CACHE.items()}
