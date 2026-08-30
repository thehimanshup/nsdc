"""Conversation-quality guardrails for citizen-facing turns.

These are non-functional requirements that sit above any department persona:
- same-language, same-modality responses
- no bot/AI self-disclosure unless explicitly configured elsewhere
- no repeated slot prompts / conversation loops
- no checklist-style interrogation
- reuse facts the citizen already provided in the conversation

The helpers are deliberately deterministic and lightweight. They do not try to
replace the LLM; they give the LLM a compact state summary and then sanitize the
few failure modes that are unacceptable in citizen-facing government channels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .models import Message


SENSITIVE_SLOT_PATTERNS: dict[str, re.Pattern] = {
    "PAN number": re.compile(r"\bpan\b", re.I),
    "Aadhaar number": re.compile(r"\baadhaar|\baadhar|आधार", re.I),
    "ration card number": re.compile(r"ration\s+card|राशन", re.I),
    "driving licence number": re.compile(r"driving\s+licen[cs]e|\bdl\b", re.I),
    "reference number": re.compile(r"reference\s+number|application\s+number|complaint\s+number|\b(?:GRV|APP|SRV|REC)-", re.I),
    "mobile number": re.compile(r"mobile\s+number|phone\s+number|मोबाइल", re.I),
    "full address": re.compile(r"\baddress\b|पता|முகவரி", re.I),
    "date of birth": re.compile(r"date\s+of\s+birth|\bdob\b|जन्म", re.I),
}

ASK_WORDS = re.compile(
    r"\b(please|share|provide|enter|tell|give|need|ask|confirm|send|type)\b|कृपया|बताइ|दीजिए|சொல்ல|தர|கொட",
    re.I,
)

CANNOT_PROVIDE = re.compile(
    r"\b(i\s*(do\s*not|don't|dont)\s*(remember|know|have)|"
    r"i\s*(can\s*not|can't|cannot|cant)\s*(share|provide|give|send)|"
    r"not\s+now|later|forgot|forget|no\s+pan|don't\s+have|dont\s+have)\b|"
    r"याद\s+नहीं|नहीं\s+पता|अभी\s+नहीं|नहीं\s+है|"
    r"தெரியாது|நினைவில்\s+இல்லை|இப்போது\s+இல்லை",
    re.I,
)

IDENTITY_DISCLOSURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bI\s+am\s+an?\s+(AI[- ]?)?(bot|chatbot|virtual assistant|AI assistant|language model)\b", re.I),
     "I am here from the department helpdesk"),
    (re.compile(r"\bI'm\s+an?\s+(AI[- ]?)?(bot|chatbot|virtual assistant|AI assistant|language model)\b", re.I),
     "I'm here from the department helpdesk"),
    (re.compile(r"\bas\s+an?\s+(AI[- ]?)?(bot|chatbot|AI assistant|language model)\b", re.I),
     "from the department helpdesk"),
    (re.compile(r"\bAI[- ]?bot\b|\bchatbot\b|\blanguage model\b", re.I),
     "department helpdesk"),
]


@dataclass
class SlotLoopState:
    slot: str = ""
    attempts: int = 0
    user_declined_or_cannot: bool = False

    @property
    def needs_alternative(self) -> bool:
        return bool(self.slot) and (self.attempts >= 3 or self.user_declined_or_cannot)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def extract_known_facts(messages: Iterable[Message], latest_user_text: str = "") -> dict[str, str]:
    """Best-effort extraction of facts the citizen already volunteered.

    This intentionally stays conservative. It is enough to prevent the most
    jarring repeats such as asking name/age/city again after the user says:
    "I am Simy Chacko, 20 years old, calling from Chennai...".
    """
    texts = [m.text or "" for m in messages if getattr(m, "role", "") == "user"]
    if latest_user_text:
        texts.append(latest_user_text)
    joined = "\n".join(texts)[-5000:]
    facts: dict[str, str] = {}

    name_match = re.search(
        r"\b(?:my name is|i am|i'm)\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})",
        joined,
        re.I,
    )
    if name_match:
        name = name_match.group(1).strip().rstrip(".,")
        # Avoid capturing service phrases like "from Chennai" as names.
        if not re.search(r"\b(from|calling|regarding|having|looking)\b", name, re.I):
            facts["name"] = name

    age_match = re.search(r"\b(\d{1,3})\s*(?:years?\s*old|yrs?\s*old|साल|वर्ष)", joined, re.I)
    if age_match:
        facts["age"] = age_match.group(1)

    city_match = re.search(
        r"\b(?:calling from|located in|from|in|at)\s+([A-Z][A-Za-z .'-]{2,40}?)(?=\s+(?:regarding|about|for)\b|[,.;]|$)",
        joined,
        re.I,
    )
    if city_match:
        facts["location"] = _norm(city_match.group(1)).rstrip(".,")

    issue_match = re.search(r"\b(?:regarding|about|for)\s+(?:a\s+)?(.{8,120})", joined, re.I)
    if issue_match:
        issue = _norm(issue_match.group(1)).rstrip(".,")
        if issue:
            facts["issue/purpose"] = issue[:120]

    return facts


def detect_slot_loop(messages: Iterable[Message], latest_user_text: str) -> SlotLoopState:
    agent_texts = [m.text or "" for m in messages if getattr(m, "role", "") == "agent"]
    latest_decline = bool(CANNOT_PROVIDE.search(latest_user_text or ""))
    best = SlotLoopState(user_declined_or_cannot=latest_decline)
    for slot, pat in SENSITIVE_SLOT_PATTERNS.items():
        attempts = 0
        for txt in agent_texts:
            if pat.search(txt or "") and ("?" in txt or ASK_WORDS.search(txt or "")):
                attempts += 1
        # Also count if the latest user names the slot while refusing it.
        if attempts and attempts > best.attempts:
            best = SlotLoopState(slot=slot, attempts=attempts, user_declined_or_cannot=latest_decline)
        elif latest_decline and pat.search(latest_user_text or ""):
            best = SlotLoopState(slot=slot, attempts=max(attempts, 1), user_declined_or_cannot=True)
    return best


def render_behavior_contract(
    *,
    channel: str,
    speak_reply: bool,
    detected_lang: str,
    known_facts: dict[str, str],
    slot_loop: SlotLoopState,
) -> str:
    modality = "live spoken call" if channel in {"twilio_voice", "livekit_app"} else (
        "voice note" if speak_reply else "text chat"
    )
    facts_line = "none confidently extracted"
    if known_facts:
        facts_line = "; ".join(f"{k}={v}" for k, v in known_facts.items())

    # Document-upload guidance is channel-aware: there is no attach button on a
    # live voice call, so we tell the caller to use the app/chat instead.
    if modality == "live spoken call":
        doc_line = (
            "- DOCUMENTS: when a service genuinely needs a document (PAN, Aadhaar, driving "
            "licence, Patta, ration card, or a photo), ask the citizen to upload a photo of it "
            "in the app chat after the call — the system reads it automatically. NEVER ask them "
            "to read out their full Aadhaar/PAN number, an OTP, or any password on the call.")
    else:
        doc_line = (
            "- DOCUMENTS: when a service genuinely needs a document (PAN, Aadhaar, driving "
            "licence, Patta, ration card, or a photo), invite the citizen to UPLOAD a photo using "
            "the attach (paperclip / 📎) button in the chat — the system reads it automatically. "
            "NEVER ask them to type or read out their full Aadhaar/PAN number, an OTP, or any "
            "password. One short invitation like \"you can attach a photo of your Aadhaar with the "
            "📎 button\" is enough.")

    alt_line = ""
    if slot_loop.needs_alternative:
        alt_line = (
            f"\n- The citizen is blocked on {slot_loop.slot} "
            f"(asked {slot_loop.attempts} time(s) or they said they cannot provide it). "
            "Do NOT ask for it again now. Offer an alternative: continue without it, "
            "explain where they can find it, save the case as pending, or connect to a human officer."
        )

    return f"""
CITIZEN EXPERIENCE NFRS — MUST FOLLOW:
- Do not reveal or imply that this is an AI, bot, chatbot, language model, or automated system unless the official product flow explicitly says to introduce that way. Speak as the department service desk/persona.
- Reply in the same language as the citizen's latest input: {detected_lang}. If using an Indian language, use its native script, not Romanised text.
- Match modality: this turn is {modality}. Voice input must get a voice-ready response; text input must stay text; live calls must be short, interruptible, and conversational.
- NEVER invent or guess a reference, ticket, grievance, application, or PPO number, nor a status or a resolution date. A reference number ONLY exists if a "SYSTEM ACTION" / "SYSTEM STATUS" note in this prompt gives you one. If you have not been given one this turn, do NOT claim something is filed and do NOT make up a number like "G-2024-00567" — say you are registering it and the reference will follow, or ask only for the one detail needed. For timelines, cite the official SLA window (e.g. 30 working days), never a number you can't verify.
- Only promise actions the system can actually do. If a matter belongs to a different (e.g. central) authority, say honestly that you will log it and guide them, rather than claiming you have already forwarded or resolved it.
- SERVE ALL OF INDIA — do NOT assume the citizen is in Tamil Nadu (or any one state). India has 28 states and 8 union territories, each with its own schemes, offices, web portals, and helplines. Your department description may use Tamil Nadu as an example, but you must adapt to the CITIZEN'S state. National helplines are the same everywhere (112 emergency, 108 ambulance, 1098 CHILDLINE, 181 women, 1091 women-police, 1930 cyber). If the answer depends on the citizen's state (a state scheme, a local office, a state portal, eligibility, state helpline) and you do NOT already know their state/UT, FIRST briefly ask "Which state or union territory are you in?" before giving state-specific details. If you only have Tamil-Nadu-specific information, say it's the Tamil Nadu example and that their state's process is similar but they should confirm via their state portal/helpline. Never present a Tamil Nadu scheme/portal as if it applies nationwide.
{doc_line}
- Never run a data-collection checklist. Do not ask sequential first-name/last-name/DOB/address questions. Ask at most ONE missing detail at a time, only if truly needed.
- Reuse facts already provided. Known facts from this conversation: {facts_line}. Do not ask for these again.
- Never loop on the same missing input. Vary wording if clarification is needed. After 3 attempts, or if the citizen says they don't remember/can't share now, switch to an alternative path.
- If the citizen gives multiple useful details in one sentence, acknowledge and continue from those details instead of re-asking.
{alt_line}
""".strip()


def sanitize_identity_disclosure(text: str) -> str:
    """Remove accidental AI/bot self-identification from citizen-facing text."""
    cleaned = text or ""
    for pat, repl in IDENTITY_DISCLOSURE_PATTERNS:
        cleaned = pat.sub(repl, cleaned)
    return cleaned


def force_alternative_if_loop(text: str, slot_loop: SlotLoopState, *, lang: str = "en-IN") -> str:
    """If the model still repeats the blocked slot prompt, replace with a safe alternative."""
    if not (slot_loop.needs_alternative and slot_loop.slot):
        return text
    pat = SENSITIVE_SLOT_PATTERNS.get(slot_loop.slot)
    if not pat or not pat.search(text or ""):
        return text
    if (lang or "").startswith("hi"):
        return "ठीक है, अभी इसके बिना आगे बढ़ते हैं। मैं मामला लंबित रख सकता हूँ, या आप चाहें तो मैं बता दूँ कि यह जानकारी कहाँ मिल सकती है।"
    if (lang or "").startswith("ta"):
        return "சரி, இப்போது அது இல்லாமலே தொடரலாம். வழக்கை நிலுவையில் வைத்துக்கொள்ளலாம், அல்லது அந்த விவரத்தை எங்கே பார்க்கலாம் என்று சொல்லலாம்."
    return (
        "No problem — we can continue without that for now. I can keep this pending, "
        "tell you where to find it, or connect you to a human officer."
    )


def postprocess_citizen_reply(text: str, slot_loop: SlotLoopState, *, lang: str = "en-IN") -> str:
    cleaned = sanitize_identity_disclosure(text)
    cleaned = force_alternative_if_loop(cleaned, slot_loop, lang=lang)
    return cleaned.strip()
