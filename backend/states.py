"""States and Union Territories registry — Phase 6d.

The platform serves all 28 Indian states and 8 Union Territories. Each
state has its own official languages, helpline numbers, and (in the next
phase) its own scheme catalog. This module is the single source of truth
for state-level configuration.

Key concepts:
  - state_code: 2-letter ISO 3166-2:IN code (TN, KA, MH, UP, WB, …)
  - primary_language: the most common official language in BCP-47 form
    suitable for Sarvam Bulbul TTS (en-IN fallback for everything)
  - additional_languages: other state-official languages a citizen may use
  - helpline_overrides: state-specific helpline numbers (e.g. CMO grievance)
  - area_codes: first-1/first-2 digit prefixes of the 10-digit MSISDN that
    have been HISTORICALLY associated with this state. NOTE: Mobile Number
    Portability means area-code → state is heuristic, not authoritative.
    We use it to make a BEST GUESS on first contact and let the citizen
    override.

Bulbul v3 supported languages (TTS):
    en-IN, hi-IN, ta-IN, te-IN, kn-IN, ml-IN, bn-IN, mr-IN, gu-IN,
    pa-IN, od-IN

Saaras v3 supported languages (STT, with auto-detect via `mode=transcribe`
or `mode=codemix` without language_code):
    Above 11 plus as-IN, ur-IN, brx-IN, doi-IN, mai-IN, mni-IN, ne-IN,
    sa-IN, sat-IN, sd-IN, ks-IN, kok-IN, hi-IN (Hindi-English code-mix)

Where a state's primary language is NOT in Bulbul (e.g. Manipuri, Assamese),
we fall back to en-IN for voice TTS but keep the language for text replies
where Sarvam-30B handles it well.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("states")


# Languages supported by Bulbul v3 for TTS. Used to decide whether the
# agent's reply can be spoken in the detected language or has to be
# softened to en-IN for the audio leg of a voice call.
BULBUL_TTS_LANGUAGES: set[str] = {
    "en-IN", "hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN",
    "bn-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN",
}

# Languages Saaras v3 can transcribe (covers all official Indian languages
# plus a handful more). With `mode="transcribe"` and no `language_code`,
# the model auto-detects from this list. `mode="codemix"` returns native
# script + English code-mix as-spoken.
SAARAS_STT_LANGUAGES: set[str] = BULBUL_TTS_LANGUAGES | {
    "as-IN", "ur-IN", "brx-IN", "doi-IN", "mai-IN", "mni-IN",
    "ne-IN", "sa-IN", "sat-IN", "sd-IN", "ks-IN", "kok-IN",
}


@dataclass
class StateInfo:
    code: str                              # "TN", "KA", "MH", "DL", ...
    name: str                              # "Tamil Nadu"
    type: str                              # "state" | "ut"
    capital: str                           # "Chennai"
    primary_language: str                  # "ta-IN" — BCP-47, Bulbul-compatible
    additional_languages: list[str] = field(default_factory=list)
    area_codes: list[str] = field(default_factory=list)
    # Helpline overrides — keyed by agent_id; falls back to central numbers
    helpline_overrides: dict[str, str] = field(default_factory=dict)
    # Display flag emoji + colour for the simulator
    emoji: str = "🇮🇳"

    @property
    def is_tts_supported(self) -> bool:
        """True if the state's primary language can be spoken by Bulbul."""
        return self.primary_language in BULBUL_TTS_LANGUAGES

    @property
    def tts_language(self) -> str:
        """Best Bulbul language for this state (falls back to en-IN)."""
        if self.primary_language in BULBUL_TTS_LANGUAGES:
            return self.primary_language
        for lang in self.additional_languages:
            if lang in BULBUL_TTS_LANGUAGES:
                return lang
        return "en-IN"

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "type": self.type,
            "capital": self.capital,
            "primary_language": self.primary_language,
            "additional_languages": self.additional_languages,
            "area_codes": self.area_codes,
            "helpline_overrides": self.helpline_overrides,
            "emoji": self.emoji,
            "tts_supported": self.is_tts_supported,
            "tts_language": self.tts_language,
        }


# ---------------------------------------------------------------------------
# All 28 states + 8 Union Territories
# ---------------------------------------------------------------------------

_STATES: dict[str, StateInfo] = {}


def _reg(s: StateInfo) -> None:
    _STATES[s.code] = s


# --- Southern states ---
_reg(StateInfo("TN", "Tamil Nadu",       "state", "Chennai",     "ta-IN", ["en-IN"],
                area_codes=["98", "97", "94", "90", "87"], emoji="🌴",
                helpline_overrides={"cmo": "1100", "ration": "1967", "water": "1916",
                                     "health": "104"}))
_reg(StateInfo("KA", "Karnataka",        "state", "Bengaluru",   "kn-IN", ["en-IN"],
                area_codes=["94", "98", "97", "80"], emoji="🌺",
                helpline_overrides={"cmo": "080-22250101", "health": "104"}))
_reg(StateInfo("KL", "Kerala",           "state", "Thiruvananthapuram", "ml-IN", ["en-IN"],
                area_codes=["94", "98", "85", "75"], emoji="🥥",
                helpline_overrides={"cmo": "155300", "health": "104"}))
_reg(StateInfo("AP", "Andhra Pradesh",   "state", "Amaravati",   "te-IN", ["en-IN"],
                area_codes=["94", "98", "85"], emoji="🌶️",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("TG", "Telangana",        "state", "Hyderabad",   "te-IN", ["en-IN", "ur-IN"],
                area_codes=["94", "97", "98", "70"], emoji="💎",
                helpline_overrides={"cmo": "1100", "health": "104"}))

# --- Western states ---
_reg(StateInfo("MH", "Maharashtra",      "state", "Mumbai",      "mr-IN", ["en-IN", "hi-IN"],
                area_codes=["98", "99", "93", "94", "70"], emoji="🦁",
                helpline_overrides={"cmo": "022-22025151", "health": "104"}))
_reg(StateInfo("GJ", "Gujarat",          "state", "Gandhinagar", "gu-IN", ["en-IN", "hi-IN"],
                area_codes=["98", "99", "97"], emoji="🦚",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("GA", "Goa",              "state", "Panaji",      "kok-IN", ["en-IN", "hi-IN", "mr-IN"],
                area_codes=["98", "94"], emoji="🏖️",
                helpline_overrides={"cmo": "1100", "health": "104"}))

# --- Northern states (Hindi belt) ---
_reg(StateInfo("UP", "Uttar Pradesh",    "state", "Lucknow",     "hi-IN", ["en-IN", "ur-IN"],
                area_codes=["94", "98", "95", "82", "70"], emoji="🕌",
                helpline_overrides={"cmo": "1076", "health": "104"}))
_reg(StateInfo("DL", "Delhi",            "ut",    "New Delhi",   "hi-IN", ["en-IN", "pa-IN", "ur-IN"],
                area_codes=["98", "99", "97", "11"], emoji="🏛️",
                helpline_overrides={"cmo": "1031", "health": "104"}))
_reg(StateInfo("HR", "Haryana",          "state", "Chandigarh",  "hi-IN", ["en-IN", "pa-IN"],
                area_codes=["94", "98", "70"], emoji="🌾",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("PB", "Punjab",           "state", "Chandigarh",  "pa-IN", ["en-IN", "hi-IN"],
                area_codes=["98", "94", "75"], emoji="🪯",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("RJ", "Rajasthan",        "state", "Jaipur",      "hi-IN", ["en-IN"],
                area_codes=["94", "98", "70"], emoji="🐪",
                helpline_overrides={"cmo": "181", "health": "104"}))
_reg(StateInfo("MP", "Madhya Pradesh",   "state", "Bhopal",      "hi-IN", ["en-IN"],
                area_codes=["94", "98", "70"], emoji="🐅",
                helpline_overrides={"cmo": "181", "health": "104"}))
_reg(StateInfo("CG", "Chhattisgarh",     "state", "Raipur",      "hi-IN", ["en-IN"],
                area_codes=["94", "98", "70"], emoji="🌳",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("BR", "Bihar",            "state", "Patna",       "hi-IN", ["en-IN", "ur-IN"],
                area_codes=["94", "98", "70", "82"], emoji="🪔",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("JH", "Jharkhand",        "state", "Ranchi",      "hi-IN", ["en-IN"],
                area_codes=["94", "98", "70"], emoji="⛰️",
                helpline_overrides={"cmo": "181", "health": "104"}))
_reg(StateInfo("UK", "Uttarakhand",      "state", "Dehradun",    "hi-IN", ["en-IN"],
                area_codes=["94", "98"], emoji="🏔️",
                helpline_overrides={"cmo": "1905", "health": "104"}))
_reg(StateInfo("HP", "Himachal Pradesh", "state", "Shimla",      "hi-IN", ["en-IN"],
                area_codes=["94", "98"], emoji="🏔️",
                helpline_overrides={"cmo": "1100", "health": "104"}))

# --- Eastern states ---
_reg(StateInfo("WB", "West Bengal",      "state", "Kolkata",     "bn-IN", ["en-IN", "hi-IN"],
                area_codes=["98", "99", "94", "70", "33"], emoji="🐯",
                helpline_overrides={"cmo": "1070", "health": "104"}))
_reg(StateInfo("OR", "Odisha",           "state", "Bhubaneswar", "od-IN", ["en-IN", "hi-IN"],
                area_codes=["94", "98", "70"], emoji="🛕",
                helpline_overrides={"cmo": "155335", "health": "104"}))
_reg(StateInfo("AS", "Assam",            "state", "Dispur",      "as-IN", ["en-IN", "hi-IN", "bn-IN"],
                area_codes=["94", "98", "70"], emoji="🦏",
                helpline_overrides={"cmo": "1100", "health": "104"}))

# --- North-eastern states (Bulbul TTS may be limited; en-IN fallback) ---
_reg(StateInfo("ML", "Meghalaya",        "state", "Shillong",    "en-IN", ["hi-IN", "as-IN"],
                area_codes=["94", "98"], emoji="🌧️"))
_reg(StateInfo("NL", "Nagaland",         "state", "Kohima",      "en-IN", ["hi-IN", "as-IN"],
                area_codes=["94", "98"], emoji="🌲"))
_reg(StateInfo("MN", "Manipur",          "state", "Imphal",      "mni-IN", ["en-IN", "hi-IN"],
                area_codes=["94", "98"], emoji="🪷"))
_reg(StateInfo("MZ", "Mizoram",          "state", "Aizawl",      "en-IN", ["hi-IN"],
                area_codes=["94", "98"], emoji="🎋"))
_reg(StateInfo("TR", "Tripura",          "state", "Agartala",    "bn-IN", ["en-IN", "hi-IN"],
                area_codes=["94", "98"], emoji="🌿"))
_reg(StateInfo("AR", "Arunachal Pradesh","state", "Itanagar",    "en-IN", ["hi-IN", "as-IN"],
                area_codes=["94", "98"], emoji="🌄"))
_reg(StateInfo("SK", "Sikkim",           "state", "Gangtok",     "ne-IN", ["en-IN", "hi-IN"],
                area_codes=["94", "98"], emoji="🏔️"))

# --- Union Territories ---
_reg(StateInfo("JK", "Jammu and Kashmir", "ut",   "Srinagar",    "ur-IN", ["en-IN", "hi-IN", "ks-IN", "doi-IN"],
                area_codes=["94", "98"], emoji="🕌",
                helpline_overrides={"cmo": "1100", "health": "104"}))
_reg(StateInfo("LA", "Ladakh",           "ut",    "Leh",         "en-IN", ["hi-IN", "ur-IN"],
                area_codes=["94", "98"], emoji="🏔️"))
_reg(StateInfo("CH", "Chandigarh",       "ut",    "Chandigarh",  "hi-IN", ["en-IN", "pa-IN"],
                area_codes=["98", "94"], emoji="🪷"))
_reg(StateInfo("PY", "Puducherry",       "ut",    "Puducherry",  "ta-IN", ["en-IN", "fr-IN", "te-IN", "ml-IN"],
                area_codes=["94", "98"], emoji="🏖️"))
_reg(StateInfo("AN", "Andaman & Nicobar", "ut",   "Port Blair",  "hi-IN", ["en-IN", "bn-IN", "ta-IN"],
                area_codes=["94"], emoji="🏝️"))
_reg(StateInfo("DH", "Dadra & Nagar Haveli, Daman & Diu", "ut", "Daman",
                "gu-IN", ["en-IN", "hi-IN", "mr-IN"], area_codes=["98"], emoji="🌊"))
_reg(StateInfo("LD", "Lakshadweep",      "ut",    "Kavaratti",   "ml-IN", ["en-IN"],
                area_codes=["98"], emoji="🌊"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Sensible default when we genuinely can't tell where the citizen is.
DEFAULT_STATE_CODE = "TN"


def all_states() -> list[StateInfo]:
    return sorted(_STATES.values(), key=lambda s: (s.type, s.name))


def get_state(code: str) -> Optional[StateInfo]:
    if not code:
        return None
    return _STATES.get(code.upper())


_PLACE_TO_STATE: dict[str, str] = {
    # Strong city/district hints used when a citizen gives location in chat.
    # Keep this conservative: only map well-known unambiguous place names.
    "noida": "UP",
    "ਨੋਇਡਾ": "UP",
    "नोएडा": "UP",
    "greater noida": "UP",
    "ghaziabad": "UP",
    "lucknow": "UP",
    "kanpur": "UP",
    "varanasi": "UP",
    "meerut": "UP",
    "agra": "UP",
    "prayagraj": "UP",
    "delhi": "DL",
    "new delhi": "DL",
    "gurugram": "HR",
    "gurgaon": "HR",
    "faridabad": "HR",
    "chandigarh": "CH",
    "mumbai": "MH",
    "pune": "MH",
    "nagpur": "MH",
    "bengaluru": "KA",
    "bangalore": "KA",
    "mysuru": "KA",
    "mysore": "KA",
    "chennai": "TN",
    "coimbatore": "TN",
    "madurai": "TN",
    "hyderabad": "TG",
    "kolkata": "WB",
    "jaipur": "RJ",
    "bhopal": "MP",
    "indore": "MP",
    "patna": "BR",
    "ahmedabad": "GJ",
    "surat": "GJ",
    "kochi": "KL",
    "thiruvananthapuram": "KL",
}


def detect_state_from_text(text: str) -> Optional[StateInfo]:
    """Infer state from explicit place names in the latest citizen text.

    This is intentionally stronger than the phone-prefix guess but narrower
    than free-form geocoding: if someone says "Sector 126 Noida", we should
    stop filing TN records and use Uttar Pradesh for this turn/profile.
    """
    if not text:
        return None
    import re
    t = text.lower()
    for place in sorted(_PLACE_TO_STATE, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(place)}(?![a-z])", t):
            return get_state(_PLACE_TO_STATE[place])
    return None


def states_list_json() -> list[dict]:
    return [s.to_dict() for s in all_states()]


# ---------------------------------------------------------------------------
# Area-code → state heuristic
# ---------------------------------------------------------------------------

# In practice India's Mobile Number Portability means area code → state is
# only a HEURISTIC. We use it for an initial guess and let the citizen
# override. Specific area codes (Delhi = 11-prefix when 11-digit, Mumbai =
# 22-prefix for landlines) help only for landlines. For mobiles, the first
# 2 digits give a soft signal:
#   98, 99 → likely metros (Mumbai/Delhi/Bangalore — overlapping)
#   94 → southern + eastern states often
#   70, 80, 75 → newer numbering
# Because the signal is weak, we maintain a strong-preference map for the
# few cases where it IS reliable, and default to the most populous state.

_STRONG_PREFIX_TO_STATE: dict[str, str] = {
    # (Best-effort. In production, integrate with DOT's TRAI registry or use
    # an HLR-lookup service like Twilio Lookup.)
    # When the first 4 digits unambiguously map to a circle:
    "9876": "PB",   # Punjab common prefix
    "9988": "PB",
    "9426": "GJ",   # Gujarat
    "9913": "GJ",
    "9840": "TN",   # Tamil Nadu Chennai
    "9486": "TN",
    "9445": "TN",
    "9620": "KA",
    "9742": "KA",
    "9847": "KL",
    "9446": "KL",
    "9999": "DL",
    "9818": "DL",
    "9821": "MH",   # Mumbai
    "9920": "MH",
    "9833": "MH",
    "9830": "WB",   # Kolkata
    "9836": "WB",
    "9437": "OR",   # Odisha
    "9938": "OR",
    "9437": "OR",
    "9839": "UP",
    "9919": "UP",
}


def detect_state_from_msisdn(msisdn: str) -> Optional[StateInfo]:
    """Best-guess the citizen's state from their phone number.

    Returns None when we genuinely can't tell — caller should then prompt
    for an explicit state. This is intentionally conservative.
    """
    if not msisdn:
        return None
    # Strip +91 / 91 / leading zero
    digits = "".join(c for c in msisdn if c.isdigit())
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) < 10:
        return None

    prefix4 = digits[:4]
    if prefix4 in _STRONG_PREFIX_TO_STATE:
        return get_state(_STRONG_PREFIX_TO_STATE[prefix4])

    # No strong match — return None and let the caller ask the citizen.
    return None


# ---------------------------------------------------------------------------
# Persistence (admin can edit per-state overrides without code change)
# ---------------------------------------------------------------------------

def load_overrides(path: Path) -> None:
    """Load per-state overrides from data/states.json (if present).

    The file is a dict keyed by state_code with a partial StateInfo dict.
    Only listed fields get overridden — anything missing keeps the in-code
    default. Useful when an operator needs to update a helpline number
    without redeploying.
    """
    if not path.exists():
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read states overrides %s: %s", path, e)
        return
    for code, ov in d.items():
        s = get_state(code)
        if not s or not isinstance(ov, dict):
            continue
        for k, v in ov.items():
            if hasattr(s, k):
                setattr(s, k, v)
    log.info("Loaded state overrides for %d states from %s", len(d), path)


def save_default_states_json(path: Path) -> None:
    """Write the in-code defaults out as a starter JSON so admin can edit."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {s.code: {"helpline_overrides": s.helpline_overrides}
                for s in all_states() if s.helpline_overrides}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
