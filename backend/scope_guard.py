"""Topical-scope guardrail — Phase 7.

`prompt_safety.py` defends against prompt INJECTION (jailbreaks). This module
defends against OFF-TOPIC use: a citizen asking a government department agent to
tell a joke, solve a puzzle, write code, role-play, do homework, recommend a
movie, etc. Each agent must stay strictly within government / civic services.

Defence in depth (this is the middle layer):
  1. Prompt-level — every agent's system prompt has a STAY-IN-ROLE / SCOPE
     section (see agents.py). Soft: a small model can still be talked past it.
  2. Input scope check (THIS module, pre-LLM) — classify the citizen turn and,
     when it is clearly out-of-scope, short-circuit with a warm, localized
     refusal. The agent LLM is never asked to fulfil the off-topic request.
  3. Output scans — prompt_safety leakage / reasoning strip (post-LLM).

The classifier is hybrid so it is multilingual AND cheap:
  - FAST-BLOCK: high-confidence off-topic patterns (EN + a few Indian
    languages). Deterministic — works offline / in mock mode, covers demos.
  - FAST-ALLOW: greetings, short follow-ups (continuation), or messages
    containing government vocabulary — never spend an LLM call or risk a block.
  - LLM classifier: a single cheap router-model JSON call for everything else.
    Biased to ALLOW when unsure, so a genuine citizen is never blocked.

Fail-open: any error (or mock mode with no deterministic hit) => in-scope.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .config import settings

log = logging.getLogger("scope_guard")


# ---------------------------------------------------------------------------
# Deterministic patterns
# ---------------------------------------------------------------------------

# HIGH-CONFIDENCE off-topic intents. Kept deliberately tight to avoid blocking
# legitimate civic queries; the LLM classifier handles the subtle cases. These
# also give a working guard in mock mode (no LLM) for the obvious demo asks.
_OFFTOPIC: list[tuple[str, str]] = [
    ("joke",       r"\b(tell|crack|share|sunao|bolo)\b[^.?!]{0,18}\bjoke|\bchutkul|நகைச்சுவை|জোক্স?|ਚੁਟਕਲ"),
    ("riddle",     r"\b(riddle|puzzle|brain\s*teaser|sudoku|crossword)\b|\bpaheli\b|புதிர்|ধাঁধা|पहेली"),
    ("creative",   r"\b(write|compose|recite|sing)\b[^.?!]{0,18}\b(poem|poetry|shayari|song|story|essay|rap|joke)\b|\bek\s+kahani\b|கவிதை\s*எழுது|कविता\s+लिख|कहानी\s+सुना"),
    ("code",       r"\b(write|generate|debug|fix|explain)\b[^.?!]{0,20}\b(code|program|programme|script|python|java(script)?|c\+\+|sql|html|leetcode|algorithm)\b"),
    ("homework",   r"\b(solve|calculate|integrate|differentiate|prove)\b[^.?!]{0,30}\b(equation|integral|derivative|maths?|homework|assignment|sum\s+of)\b"),
    ("roleplay",   r"\b(pretend|role[\s-]?play|act\s+as|imagine\s+you\s+are|you\s+are\s+now\s+a)\b[^.?!]{0,40}\b(waiter|chef|cook|friend|girlfriend|boyfriend|teacher|tutor|doctor|lawyer|celebrity|hacker|dan)\b"),
    ("recommend",  r"\b(recommend|suggest|best)\b[^.?!]{0,22}\b(movie|film|web\s*series|song|playlist|restaurant|recipe|gift|novel|book\s+to\s+read|holiday\s+destination)\b"),
    ("trivia",     r"\b(who|what|when)\s+(is|was|are|won)\b[^.?!]{0,40}\b(actor|actress|cricketer|footballer|celebrity|ipl|world\s*cup|capital\s+of|movie|film|gdp\s+of)\b"),
    ("translate_fun", r"\btranslate\b[^.?!]{0,30}\b(into|to)\s+(french|german|spanish|japanese|korean|chinese|latin)\b"),
]
_OFFTOPIC_C = [(name, re.compile(p, re.IGNORECASE)) for name, p in _OFFTOPIC]

# Government / civic vocabulary. A message containing any of these is almost
# certainly in-scope, so we allow it without an LLM call.
_GOV_VOCAB = re.compile(
    r"\b(scheme|subsidy|subsidies|pension|ration|patta|chitta|adangal|encumbrance|"
    r"\bec\b|certificate|licen[sc]e|grievance|complaint|complain|aadhaar|"
    r"pmay|pm[\s-]?kisan|kcc|mgnrega|ayushman|cmchis|ignoaps|pmmvy|"
    r"welfare|eligib|application|apply|status|track|reference|helpline|"
    r"water|sewer|sewage|tanker|bill|leak|supply|connection|"
    r"hospital|ambulance|vaccine|vaccination|blood|dialysis|phc|"
    r"land|survey\s*no|mutation|registration|relief|disaster|flood|drought|crop|farmer|mandi|msp|"
    r"licence|driving|rto|bus\s*pass|permit|fitness|vehicle|"
    r"card|allocation|onorc|seeding|portab|"
    r"housing|allotment|anganwadi|scholarship|disabilit|widow|maternity|"
    r"road|pothole|project|construction|building|department|office|portal|"
    r"officer|sarkar|sarkari|yojana|yojna|scheme|आवेदन|योजना|राशन|पेंशन|शिकायत|"
    r"திட்டம்|புகார்|ஓய்வூதியம்|ரேஷன்)\b",
    re.IGNORECASE,
)

# Greetings / pleasantries / acknowledgements — always allowed.
_GREETING = re.compile(
    r"^\s*(hi+|hey+|hello+|yo|good\s*(morning|afternoon|evening)|"
    r"vanakkam|namaste|namaskar(a|am)?|nomoshkar|sat\s*sri\s*akal|"
    r"thanks?|thank\s*you|thank\s*u|tnx|ok(ay)?|okie|yes|yeah|no|nope|"
    r"bye|goodbye|see\s*you|please|theek\s*hai|sari|sariyaga)\b[\s!.]*$",
    re.IGNORECASE,
)


@dataclass
class ScopeVerdict:
    in_scope: bool
    category: str = ""        # short label when out of scope (e.g. "joke")
    confidence: float = 0.0
    via: str = ""             # how we decided: fast_allow|fast_block|llm|error


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = """You are a strict scope classifier for an Indian government services helpdesk.
The active department is: {dept}.

Decide whether the citizen's latest message belongs in a GOVERNMENT / CIVIC services conversation.

IN_SCOPE — anything a government helpdesk should handle: schemes, subsidies, pensions, documents
(ration card, patta, licence, certificates), grievances/complaints, applications, eligibility,
status tracking, public services (water, health, transport, land, welfare, housing), civic issues,
and ordinary greetings, thanks, or clarifying follow-ups.

OUT_OF_SCOPE — requests unrelated to government services: jokes, riddles, puzzles, games, poems,
stories, songs, essays, writing or debugging code, math/homework, general trivia or quizzes,
product/movie/restaurant recommendations, role-play or "pretend to be" requests, foreign-language
translation for fun, or open-ended chit-chat purely for entertainment.

When UNSURE, choose IN_SCOPE — never block a citizen who might genuinely need help.

Return ONLY JSON, no prose: {{"inScope": true|false, "category": "<short label>", "confidence": 0.0-1.0}}"""


def _dept_of(agent_id: str) -> str:
    from .agents import get_agent
    a = get_agent(agent_id)
    return a.name if a else "Government services"


def deterministic(text: str, *, has_history: bool = False) -> Optional[ScopeVerdict]:
    """Fast, LLM-free verdict, or None when undecided (caller may use the LLM).

    Order matters: a clear off-topic intent (fast-block) wins even if the text
    also contains a government word ("joke about ration cards")."""
    t = (text or "").strip()
    if not t:
        return ScopeVerdict(True, via="fast_allow", confidence=1.0)
    for name, pat in _OFFTOPIC_C:
        if pat.search(t):
            return ScopeVerdict(False, category=name, confidence=0.9, via="fast_block")
    if _GREETING.match(t):
        return ScopeVerdict(True, via="fast_allow", confidence=0.9)
    # Short follow-up in an ongoing conversation — treat as continuation.
    if has_history and len(t.split()) <= 4:
        return ScopeVerdict(True, via="fast_allow", confidence=0.8)
    if _GOV_VOCAB.search(t):
        return ScopeVerdict(True, via="fast_allow", confidence=0.85)
    return None


async def check(text: str, *, agent_id: str,
                history: Optional[list[dict]] = None) -> ScopeVerdict:
    """Classify a citizen turn as in/out of the government-services scope.

    Returns in_scope=True unless we are confident the message is off-topic.
    Never raises — fails open to in_scope.
    """
    if not settings.scope_guard_enabled:
        return ScopeVerdict(True, via="disabled", confidence=1.0)

    det = deterministic(text, has_history=bool(history))
    if det is not None:
        return det

    # Undecided → ask the LLM, unless we're in mock mode (no real classifier).
    try:
        from .llm import get_llm, llm
        if get_llm().mock_mode:
            return ScopeVerdict(True, via="mock_allow", confidence=0.5)
        msgs = [{"role": "system",
                 "content": _CLASSIFIER_SYSTEM.format(dept=_dept_of(agent_id))}]
        if history:
            msgs.extend(history[-4:])
        msgs.append({"role": "user", "content": (text or "")[:1000]})
        raw = await llm.chat_complete(messages=msgs, json_mode=True,
                                      temperature=0.0, max_tokens=60)
        obj = json.loads(raw)
        in_scope = bool(obj.get("inScope", True))
        conf = float(obj.get("confidence", 0.5))
        category = str(obj.get("category", "") or "")
        # Only BLOCK when the model is confidently out-of-scope. Otherwise allow.
        if not in_scope and conf >= settings.scope_guard_threshold:
            return ScopeVerdict(False, category=category, confidence=conf, via="llm")
        return ScopeVerdict(True, category=category, confidence=conf, via="llm")
    except Exception as e:  # noqa: BLE001 — fail open, never block on an error
        log.debug("scope classifier failed, allowing: %s", e)
        return ScopeVerdict(True, via="error", confidence=0.0)


# ---------------------------------------------------------------------------
# Refusal text (warm, localized, steers back to the department)
# ---------------------------------------------------------------------------

def _hint_for(agent_id: str) -> str:
    from .agents import get_agent
    a = get_agent(agent_id)
    desc = (getattr(a, "description", "") or "").strip().rstrip(".")
    return desc or "grievances, schemes, documents and government services"


def _english_refusal(agent_id: str) -> str:
    from .agents import get_agent
    a = get_agent(agent_id)
    dept = a.name if a else "government services"
    return (f"Sorry, I'm the {dept} helpdesk, so I can't help with that — "
            f"but I can help with {_hint_for(agent_id)}. What do you need?")


async def refusal(*, agent_id: str, lang: str = "en-IN", category: str = "") -> str:
    """Build a warm, in-language one-line refusal that steers back to the dept.

    LIVE: generate it in the citizen's language via the LLM with NO off-topic
    content in the prompt (so it cannot be coerced into fulfilling the request).
    MOCK / English / error: a deterministic English template.
    """
    from .agents import get_agent
    a = get_agent(agent_id)
    dept = a.name if a else "government services"
    if (lang or "en-IN") == "en-IN":
        return _english_refusal(agent_id)
    try:
        from .llm import get_llm, llm
        if get_llm().mock_mode:
            return _english_refusal(agent_id)
        from .language import system_prompt_language_instruction  # for lang name
        # Reuse the language-name table indirectly via a tiny prompt; the model
        # knows the code. We pass NO citizen text — only the decline task.
        sys = (f"You are an officer at the {dept} (Indian government). A citizen "
               f"just asked for something OUTSIDE government services "
               f"({category or 'entertainment / general chit-chat'}). Reply in the "
               f"language with code '{lang}' with EXACTLY ONE short, warm sentence: "
               f"politely decline and steer them back to what you can help with "
               f"({_hint_for(agent_id)}). Do NOT fulfil their original request. "
               f"Output only the one sentence.")
        out = await llm.chat_complete(
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": "Decline politely now."}],
            temperature=0.4, max_tokens=90)
        out = (out or "").strip().strip('"')
        return out or _english_refusal(agent_id)
    except Exception as e:  # noqa: BLE001
        log.debug("refusal generation failed, using template: %s", e)
        return _english_refusal(agent_id)


__all__ = ["ScopeVerdict", "check", "refusal", "deterministic"]
