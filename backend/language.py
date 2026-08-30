"""Language auto-detection — Phase 6d.

Three input types, three strategies. All aligned with the Sarvam skills:

1. **Voice clips (citizen voice notes, live call)** — use Saaras v3 with
   `mode="codemix"` and OMIT `language_code`. Saaras auto-detects from 23
   Indian languages and returns both the transcript and the detected
   language code. This is the most accurate path because the audio carries
   pronunciation cues.

2. **Text messages (WhatsApp, simulator chat)** — script-based first pass
   (Devanagari → hi-IN, Tamil → ta-IN, Bengali → bn-IN, …) backed by the
   citizen's state-default for romanised input (`hello` from a citizen in
   Punjab maps to pa-IN). Sarvam-30B then handles the actual reply
   multilingually — we don't need to translate at this layer.

3. **Mixed / unclear** — fall back to the citizen's state primary_language,
   and as a last resort en-IN.

The detected language flows into:
  - the orchestrator's `latest_user_lang` (persisted on the citizen profile)
  - Bulbul TTS for voice replies
  - Sarvam chat system prompt ("Reply in {language}")
  - The simulator UI (shows the detected language pill on each message)
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Optional

from .states import BULBUL_TTS_LANGUAGES, SAARAS_STT_LANGUAGES, get_state

log = logging.getLogger("language")


# ---------------------------------------------------------------------------
# Script → language mapping (the cheap, fast first pass)
# ---------------------------------------------------------------------------

# Unicode ranges per script — when a string has > ~30% chars in this range
# we attribute it to the language. Ranges sourced from Unicode 15 BMP.
_SCRIPT_RANGES: list[tuple[str, str, tuple[int, int]]] = [
    # (lang_code, script_name, (codepoint_lo, codepoint_hi))
    ("hi-IN",  "Devanagari",  (0x0900, 0x097F)),  # also mr-IN, ne-IN, sa-IN
    ("bn-IN",  "Bengali",     (0x0980, 0x09FF)),  # also as-IN
    ("pa-IN",  "Gurmukhi",    (0x0A00, 0x0A7F)),
    ("gu-IN",  "Gujarati",    (0x0A80, 0x0AFF)),
    ("od-IN",  "Odia",        (0x0B00, 0x0B7F)),
    ("ta-IN",  "Tamil",       (0x0B80, 0x0BFF)),
    ("te-IN",  "Telugu",      (0x0C00, 0x0C7F)),
    ("kn-IN",  "Kannada",     (0x0C80, 0x0CFF)),
    ("ml-IN",  "Malayalam",   (0x0D00, 0x0D7F)),
    ("si-LK",  "Sinhala",     (0x0D80, 0x0DFF)),   # not used by the platform
    ("ur-IN",  "Arabic",      (0x0600, 0x06FF)),   # Urdu uses Perso-Arabic
    ("ks-IN",  "Kashmiri",    (0x0700, 0x074F)),   # Kashmiri Perso-Arabic
]


def _char_share_in_range(text: str, lo: int, hi: int) -> float:
    """What fraction of the alphabetic characters are in a Unicode range?"""
    if not text:
        return 0.0
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return 0.0
    in_range = sum(1 for c in alpha if lo <= ord(c) <= hi)
    return in_range / len(alpha)


# Common English function words. If a Latin-only text has > 25% of its
# tokens from this set, we treat it as English regardless of the citizen's
# state default. This stops "i am having soil insurance" from being
# misclassified as Tamil just because the citizen is in TN.
_ENGLISH_STOPWORDS = frozenset((
    "a an and are as at be been being by for from has have had he her him his "
    "i in into is it its me my of on or our she that the their them there "
    "these they this to us was we were what when where which who why will "
    "with would you your am do does did being been has have had can could "
    "should may might must shall ought need help want need looking apply "
    "applying applied get gets got know knows knew tell told see saw say said "
    "what's i'm don't can't won't isn't doesn't didn't hi hello hey thanks "
    "thank please sorry yes no maybe how when where why who whom which "
    "service services scheme schemes information info status check"
).split())


def _looks_english(text: str) -> bool:
    """Heuristic — is this Latin-only text actually English (not romanised
    Indic)? True when ≥ 25% of word tokens are common English stopwords."""
    words = [w.lower() for w in text.split() if w.replace("'", "").isalpha()]
    if len(words) < 2:
        # Single-word inputs are ambiguous — "hi", "namaste", "vanakkam".
        # Treat as English-friendly when it's a plain English greeting.
        if not words:
            return False
        return words[0] in _ENGLISH_STOPWORDS
    matches = sum(1 for w in words if w in _ENGLISH_STOPWORDS)
    return matches / len(words) >= 0.25


def _romanised_family_score(text: str) -> dict[str, int]:
    """Score likely romanised Indic families from Latin-script text."""
    words = [w.lower().strip(",.:;!?\"'()[]{}") for w in text.split()
             if w.replace("'", "").isalpha()]
    if not words:
        return {}
    wset = set(words)
    wset -= _ENGLISH_STOPWORDS
    return {
        "hi-IN": len(wset & _ROMANISED_HINDI_MARKERS),
        "ta-IN": len(wset & _ROMANISED_TAMIL_MARKERS),
        "bn-IN": len(wset & _ROMANISED_BENGALI_MARKERS),
        "kn-IN": len(wset & _ROMANISED_KANNADA_MARKERS),
        "mr-IN": len(wset & _ROMANISED_MARATHI_MARKERS),
        "pa-IN": len(wset & _ROMANISED_PUNJABI_MARKERS),
        "gu-IN": len(wset & _ROMANISED_GUJARATI_MARKERS),
        "te-IN": len(wset & _ROMANISED_TELUGU_MARKERS),
        "ml-IN": len(wset & _ROMANISED_MALAYALAM_MARKERS),
    }


def detect_language_from_text(text: str, *, state_default: str = "en-IN",
                                threshold: float = 0.30) -> str:
    """Detect language from a text snippet using script analysis + English
    stopword heuristic.

    Returns a BCP-47 language code. Strategy:
      1. If > `threshold` of alphabetic chars are in a single Indic script,
         use that language (Devanagari can refine to mr-IN/ne-IN/sa-IN, and
         Bengali to as-IN, based on state default).
      2. Else if the text looks English (>25% English stopwords) → en-IN.
      3. Else → `state_default` (romanised Indic is common in WhatsApp).
    """
    text = (text or "").strip()
    if not text:
        return state_default or "en-IN"

    # First pass — pick the script with the highest share above threshold.
    best_lang = None
    best_share = 0.0
    for lang, script_name, (lo, hi) in _SCRIPT_RANGES:
        s = _char_share_in_range(text, lo, hi)
        if s > best_share:
            best_share = s
            best_lang = lang

    if best_lang and best_share >= threshold:
        if best_lang == "hi-IN" and state_default in ("mr-IN", "ne-IN", "sa-IN",
                                                        "kok-IN"):
            return state_default
        if best_lang == "bn-IN" and state_default == "as-IN":
            return state_default
        if best_lang == "ur-IN" and state_default == "ks-IN":
            return state_default
        return best_lang

    # Romanised Indic often mixes English words with a few strong local cues.
    # Check this before the English stopword heuristic so code-mixed Hindi,
    # Punjabi, Tamil, etc. do not get mislabeled as plain English.
    rom = detect_romanised_indic(text)
    if rom:
        return rom

    # Romanised Indic often mixes English words with a few strong local cues.
    # Check this before the English stopword heuristic so code-mixed Hindi,
    # Punjabi, Tamil, etc. do not get mislabeled as plain English.
    rom = detect_romanised_indic(text)
    if rom:
        return rom
    if state_default and state_default != "en-IN":
        scores = _romanised_family_score(text)
        if scores:
            best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
            if best_score >= 1 and best_lang == state_default:
                return state_default

    # Latin-only text — check whether it's really English before falling
    # back to the state default. "i am having soil insurance" → English,
    # NOT Tamil even if the citizen is from TN.
    if _looks_english(text):
        return "en-IN"

    # Romanised Indic ("namaste", "vanakkam", "kya haal") → state default.
    return state_default or "en-IN"


# ---------------------------------------------------------------------------
# Voice — uses Saaras autodetect, see voice.py for the actual call
# ---------------------------------------------------------------------------

@dataclass
class VoiceDetection:
    transcript: str
    language_code: str         # what Saaras reported
    confidence: float          # 0..1 if available
    mode_used: str             # "codemix" | "transcribe"


def normalise_sarvam_language(raw: str) -> str:
    """Sarvam returns codes like 'hi-IN' or sometimes 'hi'. Standardise."""
    if not raw:
        return "en-IN"
    raw = raw.strip()
    if "-" in raw:
        return raw
    if len(raw) == 2:
        return raw + "-IN"
    return raw


def reply_language_for(detected: str, state_default: str = "en-IN") -> str:
    """Pick the language the agent should REPLY in.

    Defaults: reply in the language the citizen used. Exceptions:
      - If detected is unsupported by both chat and STT, fall back to state
      - If detected is, say, Sanskrit, Sarvam can chat in it but Bulbul
        cannot synthesise it — keep ta-IN/hi-IN reply for chat, en-IN for voice.
    """
    if not detected:
        return state_default or "en-IN"
    return detected


def tts_language_for(detected: str, state: Optional[object] = None,
                       reply_text: str = "") -> str:
    """Pick the language Bulbul should synthesise the reply in.

    Phase 6d — when `reply_text` is provided we inspect its SCRIPT first.
    If > 30% of alpha chars fall in an Indic script, use that script's
    language for Bulbul (regardless of what the citizen's earlier
    detected language was). This handles the case where Sarvam-30B
    decided on its own to reply in Hindi because the citizen mixed
    languages — we want Bulbul to match the actual reply text, not
    the original detection.

    Falls back to `detected` → `state.tts_language` → "en-IN".
    """
    if reply_text:
        # Use the same script-share analysis as text-input detection.
        best_lang = None
        best_share = 0.0
        for lang, _name, (lo, hi) in _SCRIPT_RANGES:
            s = _char_share_in_range(reply_text, lo, hi)
            if s > best_share:
                best_share = s
                best_lang = lang
        if best_lang and best_share >= 0.30:
            # Refine Devanagari for non-Hindi state defaults
            if best_lang == "hi-IN" and detected in ("mr-IN", "ne-IN", "sa-IN",
                                                      "kok-IN"):
                best_lang = detected
            if best_lang in BULBUL_TTS_LANGUAGES:
                return best_lang
    if detected in BULBUL_TTS_LANGUAGES:
        return detected
    if state and hasattr(state, "tts_language"):
        return state.tts_language
    return "en-IN"


# BCP-47 → native script name. Used to instruct the LLM to write in the
# correct script so Bulbul TTS pronounces naturally.
_NATIVE_SCRIPT = {
    "hi-IN": "Devanagari (देवनागरी)",
    "mr-IN": "Devanagari (देवनागरी)",
    "ne-IN": "Devanagari (देवनागरी)",
    "sa-IN": "Devanagari (देवनागरी)",
    "ta-IN": "Tamil (தமிழ்)",
    "te-IN": "Telugu (తెలుగు)",
    "kn-IN": "Kannada (ಕನ್ನಡ)",
    "ml-IN": "Malayalam (മലയാളം)",
    "bn-IN": "Bengali (বাংলা)",
    "as-IN": "Bengali / Assamese (অসমীয়া)",
    "gu-IN": "Gujarati (ગુજરાતી)",
    "pa-IN": "Gurmukhi (ਗੁਰਮੁਖੀ)",
    "od-IN": "Odia (ଓଡ଼ିଆ)",
    "kok-IN": "Devanagari (देवनागरी)",
    "ks-IN": "Arabic script (کٲشُر)",
    "ur-IN": "Urdu (اردو)",
}


def system_prompt_language_instruction(detected_lang: str, state_code: str = "",
                                         channel: str = "simulator") -> str:
    """A one-liner for the system prompt that nudges the LLM's reply language.

    Phase 6d voice-quality fix — when channel is 'voice' (or any call-leg
    channel), we strongly require NATIVE SCRIPT in the reply. Bulbul TTS
    pronounces Devanagari/Tamil/Bengali natively but mangles Romanised
    Hindi like "namaste mera naam" because it tries to read those as
    English phonemes. Writing in Devanagari "नमस्ते मेरा नाम" gives
    natural-sounding output.
    """
    lang_names = {
        "en-IN": "English",
        "hi-IN": "Hindi", "ta-IN": "Tamil", "te-IN": "Telugu",
        "kn-IN": "Kannada", "ml-IN": "Malayalam", "bn-IN": "Bengali",
        "mr-IN": "Marathi", "gu-IN": "Gujarati", "pa-IN": "Punjabi",
        "od-IN": "Odia", "as-IN": "Assamese", "ur-IN": "Urdu",
        "ne-IN": "Nepali", "sa-IN": "Sanskrit", "kok-IN": "Konkani",
        "mni-IN": "Manipuri", "ks-IN": "Kashmiri",
    }
    name = lang_names.get(detected_lang, "")
    is_voice = channel in ("voice", "livekit", "twilio_voice")

    if detected_lang == "en-IN":
        return ("Reply in plain English. Don't translate to any Indian "
                "language unless the citizen explicitly uses one. The latest "
                "citizen message is the language authority: ignore earlier "
                "turns, the department name, and the state when choosing this "
                "turn's reply language.")

    if name and is_voice:
        # Voice channel — explicit native-script requirement so Bulbul TTS
        # pronounces every word naturally instead of stumbling through
        # Romanised text.
        script = _NATIVE_SCRIPT.get(detected_lang, name)
        return (
            f"This is a VOICE CALL. The citizen spoke in {name}. "
            f"Reply in {name} written in {script} — do NOT romanise. "
            f"FORBIDDEN: writing Hindi like 'aap kaise hain' in Latin "
            f"letters. REQUIRED: write 'आप कैसे हैं' in {script}. "
            f"Proper nouns (scheme names, URLs, IDs) may stay in Latin — "
            f"otherwise use {script} for EVERY word. The latest user utterance "
            f"overrides older turns and any state default. "
            f"Keep replies short — 1-2 sentences — for natural speech."
        )

    if name:
        # Chat channel — same script requirement as voice. The Romanised-
        # Hindi style ("Samajh gayi, aapko jaana hoga") is common in
        # WhatsApp culture but it confuses TTS and reads worse than
        # proper Devanagari for citizens who can read their native script.
        script = _NATIVE_SCRIPT.get(detected_lang, "native script")
        return (
            f"The citizen wrote in {name} (or code-mixed with English). "
            f"Reply in the SAME language. If you choose to reply in "
            f"{name}, you MUST write it in {script}. "
            f"NEVER write {name} in Latin letters (no 'namaste mera naam', "
            f"no 'aap kaise hain', no 'samajh gayi') — always use {script}. "
            f"Proper nouns and URLs may stay in Latin script. "
            f"If the citizen wrote in English, reply in English. The latest "
            f"user message always wins over history or state defaults."
        )
    return ("Reply in the same language and tone the citizen used. "
            "If replying in an Indian language, use its native script — "
            "never Romanised letters.")


# ---------------------------------------------------------------------------
# Phase 6d — Romanised Indic detector + Sarvam-Translate fallback
# ---------------------------------------------------------------------------
#
# Even with stricter system-prompt instructions, Sarvam-30B sometimes
# slips into Romanised Hindi ("aapko jaana hoga", "samajh gayi"). This
# is jarring in chat AND breaks Bulbul TTS (it pronounces Latin letters
# as English phonemes). When we detect it, we transliterate the reply
# to Devanagari via Sarvam-Translate BEFORE handing to Bulbul.

# Common Hindi function words that, in Romanised form, betray that the
# text is Romanised Hindi (not English). >= 2 of these in the text means
# we should transliterate.
_ROMANISED_HINDI_MARKERS = frozenset((
    "hai hain ho ka ki ke ko se me mein par bhi nahi nahin nahi to "
    "aap aapka aapko aapne aapse mera mere meri mujhe humne hum "
    "kya kyun kyon kab kahan kaise kaun jab jahan ji haan tum tumhara "
    "kar karo karna karein karein kiya karta karti karte "
    "jaa jana jaata jaati gaya gayi rahe rahi raha sake sakte sakti "
    "vala wala wale wali ke liye ke saath ke baad ke pehle "
    "samajh shikayat sarkar sarkari yojana scheme thoda zyada bahut "
    "hoga hogi honge thi tha thoda accha theek namaste namaskar dhanyavad "
    "kripya please apna apne apni apke apse banaaye matlab fir phir abhi "
    "shubh shaam din raat sham subah sham"
).split())

# Similar markers for other major Indic languages (rough — enough to
# detect "Vanakkam naan vivasayi" Tamil-roman, "Namaskara naanu" Kannada,
# "Nomoshkar ami" Bengali, etc.).
_ROMANISED_TAMIL_MARKERS = frozenset((
    "vanakkam naan enaku enna eppadi nandri pannunga thirumba "
    "kekkanum velai vada vendam ondrey rendu moonu vivasayi"
).split())

_ROMANISED_BENGALI_MARKERS = frozenset((
    "nomoshkar ami tomar tumi apnar apni amake amar shob kichu kemon "
    "achen achi ache kothay kintu thik chai chao chacchi"
).split())

_ROMANISED_KANNADA_MARKERS = frozenset((
    "namaskara naanu neevu ninnage avara horagade hottu kelladu nodi"
).split())

_ROMANISED_MARATHI_MARKERS = frozenset((
    "namaskar mi tumcha tumhi mala maza maazi kase kashi aahe aahet "
    "kuthe kashasathi karaayla geli geleli zaali zaalo"
).split())

# Romanised Punjabi. NOTE: Punjabi shares many function words with Hindi
# ("ki", "ho", "ke", "nu"), so this list deliberately holds only DISTINCTIVE
# Punjabi words that do NOT appear in the Hindi list — words like "tussi"
# (you), "menu/mainu" (to me), "sakde" (can), "dasa/dasso" (tell), "kive"
# (how), "vich" (in). Without this, "ki tussi menu ... dasa sakde ho?" matched
# only the Hindi "ki"+"ho" and was misread as Hindi.
_ROMANISED_PUNJABI_MARKERS = frozenset((
    "tussi tusi tuhanu tuhada tuhade tuhadi menu mainu asi assi saanu sade "
    "sakde sakda sakdi dasso daso dass dasa kive kiven kida kiddan kithe kado "
    "jado ohna ohnu chahida changa varga varge vargi naal vich hega haiga pind"
).split())

# Romanised Gujarati — distinctive words ("kem cho", "tame", "mane", "nathi")
# that don't appear in the Hindi list, so code-mixed Gujarati isn't misread.
_ROMANISED_GUJARATI_MARKERS = frozenset((
    "kem cho chho tame tamne tamaru tamari mane maru mari chhe nathi shu "
    "kevi ketlu kyaare saru saaru joiye karvu jaavu aavu chhie hovu "
    "namaste aabhar madad araji fariyaad yojana mara"
).split())

# Romanised Telugu — distinctive ("nenu", "meeru", "naaku", "kavali", "cheppandi").
_ROMANISED_TELUGU_MARKERS = frozenset((
    "namaskaram nenu meeru meeku naaku naku nuvvu ela unnaru emiti emi "
    "kavali kaavali cheppandi cheyyandi ekkada enduku ledu manchi "
    "dhanyavadalu samasya nundi gurinchi"
).split())

# Romanised Malayalam — distinctive ("njan", "ningal", "enikku", "venam").
_ROMANISED_MALAYALAM_MARKERS = frozenset((
    "namaskaram njan ningal enikku engane undu entha enthu venam parayu "
    "evide illa nanni ente cheyyu aanu veno"
).split())

# BCP-47 -> that language's romanised marker set. Used for STICKY romanised
# detection: when a conversation is already in an Indic language, a SINGLE
# marker word is enough to keep it there (so "theek hai" / "haan ji" in an
# ongoing Hindi thread don't flip the reply to English).
_ROMANISED_MARKERS_BY_LANG: dict[str, frozenset] = {
    "hi-IN": _ROMANISED_HINDI_MARKERS,
    "ta-IN": _ROMANISED_TAMIL_MARKERS,
    "bn-IN": _ROMANISED_BENGALI_MARKERS,
    "kn-IN": _ROMANISED_KANNADA_MARKERS,
    "mr-IN": _ROMANISED_MARATHI_MARKERS,
    "pa-IN": _ROMANISED_PUNJABI_MARKERS,
    "gu-IN": _ROMANISED_GUJARATI_MARKERS,
    "te-IN": _ROMANISED_TELUGU_MARKERS,
    "ml-IN": _ROMANISED_MALAYALAM_MARKERS,
}

# A single hit in these subsets is usually enough to trust romanized Indic
# over English, even when the sentence is code-mixed.
_ROMANISED_STRONG_MARKERS_BY_LANG: dict[str, frozenset] = {
    "hi-IN": frozenset(
        "namaste kripya shikayat sarkar sarkari yojana aapka aapko aapne "
        "mujhe mera meri mere apna apne apni kya kaise kyun kyon nahi nahin "
        "abhi phir samajh batao chahiye karo karna madad fasal paani kisan "
        "bhai pareshani aavedan samasya yahan wahan lekin".split()
    ),
    "ta-IN": frozenset(
        "vanakkam naan enaku eppadi vendum venum pannunga panunga irukku "
        "iruka illa enna unga inga enga kekkanum sollunga thirumba kudunga "
        "pothum".split()
    ),
    "bn-IN": frozenset(
        "nomoshkar ami apnar amake amar kemon ache kothay kichu chai".split()
    ),
    "kn-IN": frozenset(
        "namaskara naanu neevu nimage kivvu kivva bega madu madutte".split()
    ),
    "mr-IN": frozenset(
        "namaskar mi tumhi mala maza mazi kase kashi aahe aahet kara "
        "karayla zala zaala".split()
    ),
    "pa-IN": frozenset(
        "tussi tusi tuhanu menu mainu assi saanu sakde sakda sakdi dasso "
        "daso dass kive kiven kiddan hega haiga pind sat sri akal veera veere "
        "hun naal nal changa bhala ji".split()
    ),
    "gu-IN": frozenset(
        "kem cho chho tamne tamaru maru mari nathi kevi joiye "
        "aabhar saru chhie".split()
    ),
    "te-IN": frozenset(
        "namaskaram nenu meeru meeku naaku nuvvu ela unnaru emiti kavali "
        "cheppandi enduku dhanyavadalu".split()
    ),
    "ml-IN": frozenset(
        "namaskaram njan ningal enikku engane entha venam parayu evide "
        "nanni ente".split()
    ),
}


def detect_romanised_indic(text: str) -> str:
    """Return BCP-47 code if `text` looks like Romanised Indic.

    Returns "" if the text is plain English or already native-script.
    Detection is conservative, but a single strong marker can still win.
    """
    if not text:
        return ""
    # Quick reject: if the text already has Indic script chars, skip.
    for c in text:
        cp = ord(c)
        if 0x0900 <= cp <= 0x0DFF or 0x0600 <= cp <= 0x06FF:
            return ""
    words = [w.lower().strip(",.:;!?\"'()[]{}") for w in text.split()
              if w.replace("'", "").isalpha()]
    if not words:
        return ""
    wset = set(words)
    # Drop common English words BEFORE counting Indic markers. Several marker
    # lists contain words that are also plain English ("me", "please", "to"),
    # so a sentence like "can you tell me ... please" was scoring 2 Hindi hits
    # and being misread as Hindi. A real romanised-Indic sentence keeps plenty
    # of distinctive non-English markers, so this only strips false positives.
    wset -= _ENGLISH_STOPWORDS
    strong_counts = {
        lang: len(wset & _ROMANISED_STRONG_MARKERS_BY_LANG.get(lang, frozenset()))
        for lang in _ROMANISED_MARKERS_BY_LANG
    }
    best_strong_lang, best_strong_count = max(
        strong_counts.items(), key=lambda kv: kv[1]
    )
    if best_strong_count >= 1:
        return best_strong_lang
    if len(words) < 2:
        return ""
    counts = {
        "hi-IN": len(wset & _ROMANISED_HINDI_MARKERS),
        "ta-IN": len(wset & _ROMANISED_TAMIL_MARKERS),
        "bn-IN": len(wset & _ROMANISED_BENGALI_MARKERS),
        "kn-IN": len(wset & _ROMANISED_KANNADA_MARKERS),
        "mr-IN": len(wset & _ROMANISED_MARATHI_MARKERS),
        "pa-IN": len(wset & _ROMANISED_PUNJABI_MARKERS),
        "gu-IN": len(wset & _ROMANISED_GUJARATI_MARKERS),
        "te-IN": len(wset & _ROMANISED_TELUGU_MARKERS),
        "ml-IN": len(wset & _ROMANISED_MALAYALAM_MARKERS),
    }
    # Romanised Punjabi shares function words (ki, ho, ke, nu) with Hindi, so
    # the Hindi count can win on those alone. The Punjabi list holds only
    # DISTINCTIVE words, so whenever >=2 of them fire we trust Punjabi over a
    # Hindi count built from the shared words.
    if counts["pa-IN"] >= 2 and counts["pa-IN"] >= counts["hi-IN"]:
        return "pa-IN"
    best_lang, best_count = max(counts.items(), key=lambda kv: kv[1])
    if best_count >= 2:
        return best_lang
    return ""


def resolve_turn_language(text: str, current_lang: str = "en-IN",
                          state_default: str = "en-IN") -> str:
    """Per-turn language resolution — reply in the language of the LATEST
    message, switching CONFIDENTLY while staying sticky only for genuinely
    ambiguous tokens.

    Phase 6g shipped a sticky version that fixed flip-flopping on 'ok'/'123'
    but over-corrected: it only treated Latin text as English when >=25% of
    its words were English STOPWORDS. So content questions with few filler
    words ('pension amount details', 'show pending application') failed the
    English test, fell through, and inherited the previous Indic language —
    i.e. a citizen who started in Hindi and then typed a plain English
    question kept getting Hindi replies.

    New strategy — switch on ANY clear content signal; keep the current
    language ONLY for ultra-short ambiguous acknowledgments:
      1. native Indic script (>=30%)            -> that language (strongest)
      2. romanised Indic markers (>=2 distinct)  -> that language
         ... or a SINGLE marker if the thread is already in that language
      3. ultra-short / non-word token            -> keep current language
         ('ok', 'yes', 'hmm', '123' don't flip a Hindi thread to English;
          a lone clearly-English word like 'hello'/'thanks' still switches)
      4. Latin script, >=2 real words, not romanised Indic -> English
         (stopword density is a positive hint, NOT a requirement — an
          English content question is unmistakable to a human even with
          zero filler words)
    """
    text = (text or "").strip()
    cur = current_lang or state_default or "en-IN"
    if not text:
        return cur

    # 1. Native script — strongest signal.
    best_lang, best_share = None, 0.0
    for lang, _name, (lo, hi) in _SCRIPT_RANGES:
        s = _char_share_in_range(text, lo, hi)
        if s > best_share:
            best_share, best_lang = s, lang
    if best_lang and best_share >= 0.30:
        if best_lang == "hi-IN" and state_default in ("mr-IN", "ne-IN", "sa-IN",
                                                      "kok-IN"):
            return state_default
        if best_lang == "bn-IN" and state_default == "as-IN":
            return state_default
        if best_lang == "ur-IN" and state_default == "ks-IN":
            return state_default
        return best_lang

    # Latin-script from here. Tokenise into real word tokens once.
    words = [w.lower().strip(",.:;!?\"'()[]{}") for w in text.split()
             if w.replace("'", "").isalpha()]

    # 2. Romanised Indic. Conservative globally (>=2 markers via
    #    detect_romanised_indic). When the conversation is ALREADY in an
    #    Indic language, a single marker word is enough to keep it there.
    rom = detect_romanised_indic(text)
    if rom:
        return rom
    if cur not in ("en-IN", "") and words:
        sticky = _ROMANISED_MARKERS_BY_LANG.get(cur)
        # Ignore markers that are ALSO plain English words ("please", "to",
        # "me") — a lone such word in an English sentence must not pin the
        # thread to the Indic language.
        if sticky and ((set(words) & sticky) - _ENGLISH_STOPWORDS):
            return cur

    # 3. Ultra-short / non-word — too ambiguous to switch on. Keep current,
    #    except a lone clearly-English greeting/stopword which DOES switch.
    if len(words) <= 1:
        if len(words) == 1 and words[0] in _ENGLISH_STOPWORDS:
            return "en-IN"
        return cur

    # 4. Latin script, >=2 real words, ruled out as romanised Indic -> English.
    return "en-IN"


async def transliterate_to_native(text: str, target_lang: str) -> str:
    """Call Sarvam-Translate to convert Romanised Indic text → native script.

    Per the Sarvam Translate skill: `sarvam-translate:v1` accepts any
    input language and outputs the target. For Romanised Hindi the
    src is heuristically en-IN (Latin script signals English to Sarvam).
    Output is in the target's native script.

    Returns the transliterated text on success, or the original text on
    any failure (so the agent's reply is never lost).
    """
    if not text or not target_lang:
        return text
    from .config import settings
    if not settings.sarvam_api_key:
        return text   # mock mode — can't transliterate
    import httpx
    from .http_client import httpx_client_kwargs

    payload = {
        "input": text[:1500],
        "source_language_code": "en-IN",
        "target_language_code": target_lang,
        "model": "sarvam-translate:v1",
    }
    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Content-Type": "application/json",
    }
    url = f"{settings.sarvam_base_url}/translate"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=20.0),
                                       **httpx_client_kwargs()) as c:
            r = await c.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                log.warning("Sarvam translate HTTP %d for romanised→native: %s",
                            r.status_code, r.text[:200])
                return text
            obj = r.json()
            out = obj.get("translated_text") or obj.get("output") or ""
            return out.strip() or text
    except Exception as e:
        log.warning("Sarvam translate call failed: %s", e)
        return text


def detect_script_language(text: str, *, threshold: float = 0.30) -> str:
    """Return the dominant native-script language in text, or empty string."""
    best_lang = ""
    best_share = 0.0
    for lang, _name, (lo, hi) in _SCRIPT_RANGES:
        s = _char_share_in_range(text or "", lo, hi)
        if s > best_share:
            best_share, best_lang = s, lang
    return best_lang if best_share >= threshold else ""


async def translate_to_language(text: str, target_lang: str,
                                source_lang: str = "") -> str:
    """Translate reply text to the requested BCP-47 language.

    Used as a last-mile guard when the LLM ignores the latest turn language
    because prior chat history was in another language.
    """
    if not text or not target_lang:
        return text
    from .config import settings
    if not settings.sarvam_api_key:
        return text
    import httpx
    from .http_client import httpx_client_kwargs

    payload = {
        "input": text[:1500],
        "source_language_code": source_lang or detect_script_language(text) or "en-IN",
        "target_language_code": target_lang,
        "model": "sarvam-translate:v1",
    }
    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Content-Type": "application/json",
    }
    url = f"{settings.sarvam_base_url}/translate"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=20.0),
                                       **httpx_client_kwargs()) as c:
            r = await c.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                log.warning("Sarvam translate HTTP %d for reply language guard: %s",
                            r.status_code, r.text[:200])
                return text
            obj = r.json()
            out = obj.get("translated_text") or obj.get("output") or ""
            return out.strip() or text
    except Exception as e:
        log.warning("Reply language guard translate failed: %s", e)
        return text


async def enforce_reply_language(text: str, target_lang: str) -> str:
    """Correct obvious reply-language drift before saving/sending.

    The most common bug is: latest user turn is English, but earlier history
    was Punjabi/Tamil/Hindi, so the model continues in that prior language.
    """
    if not text or not target_lang:
        return text
    script_lang = detect_script_language(text)
    if target_lang == "en-IN" and script_lang and script_lang != "en-IN":
        return await translate_to_language(text, "en-IN", source_lang=script_lang)
    return text


def _language_family(lang: str) -> str:
    lang = (lang or "").strip().lower()
    families = {
        "hi-in": "devanagari",
        "mr-in": "devanagari",
        "ne-in": "devanagari",
        "sa-in": "devanagari",
        "kok-in": "devanagari",
        "bn-in": "bengali",
        "as-in": "bengali",
        "pa-in": "gurmukhi",
        "gu-in": "gujarati",
        "od-in": "odia",
        "ta-in": "tamil",
        "te-in": "telugu",
        "kn-in": "kannada",
        "ml-in": "malayalam",
        "ur-in": "arabic",
        "ks-in": "arabic",
        "en-in": "latin",
    }
    return families.get(lang, lang or "latin")


def choose_response_for_language(
    candidates: list[str],
    *,
    target_lang: str,
    user_text: str = "",
) -> str:
    """Pick the mock reply that best matches the latest turn language."""
    if not candidates:
        return ""
    target = (target_lang or "en-IN").strip() or "en-IN"
    user_tokens = {
        w.lower() for w in re.findall(r"\b\w+\b", user_text or "")
        if len(w) > 1
    }

    scored: list[tuple[int, str]] = []
    for text in candidates:
        cand_lang = resolve_turn_language(
            text or "", current_lang=target, state_default=target,
        )
        score = 0
        if cand_lang == target:
            score += 5
        if _language_family(cand_lang) == _language_family(target):
            score += 3
        if target == "en-IN" and cand_lang == "en-IN":
            score += 1
        if target != "en-IN":
            native = detect_script_language(text or "")
            if native and _language_family(native) == _language_family(target):
                score += 2
        if user_tokens:
            cand_tokens = {
                w.lower() for w in re.findall(r"\b\w+\b", text or "")
                if len(w) > 1
            }
            score += min(2, len(user_tokens & cand_tokens))
        scored.append((score, text))

    best_score = max(score for score, _ in scored)
    top = [text for score, text in scored if score == best_score]
    if best_score <= 0:
        return random.choice(candidates)
    return random.choice(top)


__all__ = [
    "detect_language_from_text",
    "VoiceDetection",
    "normalise_sarvam_language",
    "reply_language_for",
    "tts_language_for",
    "system_prompt_language_instruction",
    "detect_romanised_indic",
    "detect_script_language",
    "resolve_turn_language",
    "transliterate_to_native",
    "translate_to_language",
    "enforce_reply_language",
    "choose_response_for_language",
]
