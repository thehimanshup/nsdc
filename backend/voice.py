"""Voice clients — Saaras V3 (STT) + Bulbul V3 (TTS).

LIVE mode: calls Sarvam REST endpoints.
MOCK mode: returns canned transcripts + a synthesized sine-wave WAV
           (so the simulator can play *something* and we exercise the
           full audio round-trip without API calls).

Format notes (from Sarvam skills):
  - Saaras REST supports audio up to 30s. For longer, use Batch API.
  - Auth header: `api-subscription-key` (NOT Bearer).
  - Bulbul v3 default speaker: `shubh`. `pitch` and `loudness` are rejected.
    Only `pace` (0.5-2.0) works.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import random
import struct
import wave
from dataclasses import dataclass

log = logging.getLogger("voice")

import httpx

from .config import settings
from .http_client import httpx_client_kwargs
from .sarvam_client import detect_language_naive


@dataclass
class STTResult:
    transcript: str
    language: str
    duration_s: float
    mock: bool = False


@dataclass
class TTSResult:
    audio_bytes: bytes
    mime: str
    duration_s: float
    mock: bool = False
    error: str = ""        # populated when LIVE call failed (e.g. invalid speaker)
    http_status: int = 0


# ---------------------------------------------------------------------------
# Saaras V3 — Speech-to-Text
# ---------------------------------------------------------------------------

async def stt_transcribe(audio_bytes: bytes, mime_type: str = "audio/wav",
                            mode: str = "transcribe",
                            language_hint: str = "") -> STTResult:
    """Transcribe a short audio clip with Saaras v3.

    Phase 6d voice-quality fix — default mode changed from "codemix" to
    "transcribe" because codemix often returned Romanised Hindi
    ("namaste mera naam") instead of native Devanagari, which then made
    Bulbul TTS pronounce English-style — unnatural for the citizen.

    Saaras v3 modes (per the Sarvam STT skill):
      - "transcribe" : NATIVE SCRIPT of the detected language (default now)
      - "translate"  : English translation of native audio
      - "verbatim"   : exact phonetic transcript
      - "translit"   : always romanised
      - "codemix"    : preserves code-mixed Hinglish / Tanglish as-spoken

    `language_hint` (Phase 6d): when set to a BCP-47 code (e.g. "hi-IN"
    from the citizen's state primary_language), Saaras biases detection
    toward that language. Helpful when the citizen is in a known
    monolingual state and we want to lock detection.
    """
    if settings.mock_mode or not audio_bytes:
        if not settings.allow_mock_providers:
            return STTResult(
                transcript="[STT unavailable: SARVAM_API_KEY is required and mock fallback is disabled]",
                language="en-IN", duration_s=0.0, mock=False,
            )
        return await _stt_mock()

    files = {"file": ("audio.wav", audio_bytes, mime_type)}
    data = {"model": "saaras:v3", "mode": mode}
    if language_hint:
        # Sarvam expects e.g. "hi-IN". An empty string would force auto-detect.
        data["language_code"] = language_hint
    headers = {"api-subscription-key": settings.sarvam_api_key}
    url = f"{settings.sarvam_base_url}/speech-to-text"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), **httpx_client_kwargs()) as c:
            r = await c.post(url, files=files, data=data, headers=headers)
            if r.status_code >= 400:
                log.error("Sarvam STT HTTP %d: %s | audio_bytes=%d mime=%s mode=%s hint=%s",
                          r.status_code, r.text[:200], len(audio_bytes), mime_type, mode, language_hint)
                return STTResult(
                    transcript=f"[Sarvam STT HTTP {r.status_code}: {r.text[:80]}]",
                    language="en-IN", duration_s=0.0, mock=False,
                )
            obj = r.json()
            transcript = obj.get("transcript", "") or obj.get("text", "")
            raw_lang = obj.get("language_code") or ""
            from .language import normalise_sarvam_language, detect_language_from_text
            lang = normalise_sarvam_language(raw_lang) if raw_lang \
                   else detect_language_from_text(transcript)
            log.info("Sarvam STT OK: audio_bytes=%d transcript_len=%d lang=%s mode=%s hint=%s",
                     len(audio_bytes), len(transcript), lang, mode, language_hint or "(auto)")
            return STTResult(
                transcript=transcript, language=lang,
                duration_s=float(obj.get("duration", 0.0) or 0.0),
                mock=False,
            )
    except Exception as e:
        log.exception("Sarvam STT call raised: %s", e)
        return STTResult(
            transcript=f"[STT error: {e.__class__.__name__}: {str(e)[:80]}]",
            language="en-IN", duration_s=0.0, mock=False,
        )


async def _stt_mock() -> STTResult:
    await asyncio.sleep(0.4 + random.random() * 0.4)
    samples = [
        ("வணக்கம், எனக்கு பட்டா பற்றி தெரிய வேண்டும்.", "ta-IN"),
        ("मेरे राशन कार्ड में नाम कैसे जोड़ें?", "hi-IN"),
        ("నా డ్రైవింగ్ లైసెన్స్ ఎప్పుడు రెన్యూ చేయాలి?", "te-IN"),
        ("আমার এলাকায় জল সরবরাহ কখন আসবে?", "bn-IN"),
        ("Hello, I need help with my Patta application.", "en-IN"),
        ("ஆம்புலன்ஸ் எண் என்ன?", "ta-IN"),
        ("KCC ke liye kya documents chahiye?", "en-IN"),
    ]
    text, lang = random.choice(samples)
    return STTResult(transcript=text, language=lang, duration_s=4.5, mock=True)


# ---------------------------------------------------------------------------
# Bulbul V3 — Text-to-Speech
# ---------------------------------------------------------------------------

# Real bulbul:v3 voices per Sarvam docs. Anything outside this set must be
# remapped before we hit the API — otherwise Bulbul returns HTTP 400 and the
# caller silently falls back to a chime.
VALID_V3_VOICES: set[str] = {
    # Male
    "shubh", "aditya", "rahul", "rohan", "varun", "amit", "dev", "kabir",
    "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "vijay",
    "mohit", "rehan", "soham", "aayan", "manan", "sumit", "gokul",
    "ratan", "amit",
    # Female
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali", "tanya",
}

# Map known-bad voice names (v2-only or fabricated) to a sensible v3 default,
# so existing agents.json files heal themselves the first time TTS runs.
LEGACY_VOICE_REMAP: dict[str, str] = {
    # v2-only voices that don't work on bulbul:v3
    "manisha": "simran",   # both female + authoritative
    "vidya":   "priya",    # both female + warm/calm
    "anushka": "ritu",     # generic female
    "arya":    "kavya",    # generic female
    "abhilash": "rahul",   # male professional
    "karun":    "rohan",   # male calm
    "hitesh":   "shubh",   # male default
    # Fabricated voices from an earlier Phase 5 build
    "arjun":  "rahul",
    "anjali": "ritu",
    "amol":   "amit",
    "diya":   "neha",
}


def _resolve_speaker(speaker: str) -> str:
    """Map any incoming speaker name to a real bulbul:v3 voice.
    Returns shubh as the universal fallback."""
    if speaker in VALID_V3_VOICES:
        return speaker
    remapped = LEGACY_VOICE_REMAP.get(speaker)
    if remapped:
        log.warning("Legacy/invalid voice '%s' remapped to '%s' for bulbul:v3.",
                    speaker, remapped)
        return remapped
    log.warning("Unknown voice '%s' — falling back to default 'shubh'.", speaker)
    return "shubh"


# Curated 3-voice pools. Every agent resolves to one of these choices for a
# given conversation or call, so the voice feels fresh without changing
# mid-turn.
VOICE_VARIANT_POOLS: dict[str, list[str]] = {
    "shubh": ["shubh", "rahul", "rohan"],
    "aditya": ["aditya", "dev", "varun"],
    "rahul": ["rahul", "kabir", "rohan"],
    "rohan": ["rohan", "rahul", "varun"],
    "amit": ["amit", "kabir", "shubh"],
    "dev": ["dev", "aditya", "shubh"],
    "kabir": ["kabir", "amit", "rahul"],
    "varun": ["varun", "aditya", "sunny"],
    "ritu": ["ritu", "priya", "pooja"],
    "priya": ["priya", "neha", "ishita"],
    "neha": ["neha", "priya", "ritu"],
    "pooja": ["pooja", "shreya", "suhani"],
    "simran": ["simran", "pooja", "ritu"],
    "kavya": ["kavya", "shruti", "rupali"],
    "ishita": ["ishita", "priya", "neha"],
    "shreya": ["shreya", "tanya", "rupali"],
    "roopa": ["roopa", "pooja", "priya"],
    "tanya": ["tanya", "shreya", "suhani"],
    "shruti": ["shruti", "kavya", "rupali"],
    "suhani": ["suhani", "pooja", "shreya"],
    "kavitha": ["kavitha", "priya", "pooja"],
    "rupali": ["rupali", "shreya", "neha"],
}

_MALE_VARIANT_POOL = [
    "shubh", "aditya", "rahul", "rohan", "varun", "amit", "dev", "kabir",
    "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "vijay",
    "mohit", "rehan", "soham", "aayan", "manan", "sumit", "gokul", "ratan",
]

_FEMALE_VARIANT_POOL = [
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
]


def voice_pool_for(primary_voice: str) -> list[str]:
    """Return a stable 3-voice pool for the given Bulbul speaker."""
    resolved = _resolve_speaker((primary_voice or "").strip().lower())
    pool = VOICE_VARIANT_POOLS.get(resolved)
    if not pool:
        source = (_FEMALE_VARIANT_POOL
                  if resolved in _FEMALE_VARIANT_POOL
                  else _MALE_VARIANT_POOL)
        if resolved in source:
            idx = source.index(resolved)
            pool = [source[idx], source[(idx + 1) % len(source)],
                    source[(idx + 2) % len(source)]]
        else:
            pool = [resolved, source[0], source[1]]
    cleaned: list[str] = []
    for v in pool:
        rv = _resolve_speaker(v)
        if rv not in cleaned:
            cleaned.append(rv)
    while len(cleaned) < 3:
        cleaned.append(_resolve_speaker(primary_voice))
    return cleaned[:3]


def select_voice_variant(primary_voice: str, *, seed: str = "",
                         voice_pool: list[str] | None = None) -> str:
    """Pick one voice from the configured pool.

    When ``seed`` is provided the selection is deterministic for that
    conversation/call, which keeps a speaker stable across turns while still
    varying from one citizen session to the next.
    """
    pool = [v for v in (voice_pool or voice_pool_for(primary_voice)) if v]
    if not pool:
        return _resolve_speaker(primary_voice)
    if len(pool) == 1:
        return _resolve_speaker(pool[0])
    if seed:
        rng = random.Random(f"{seed}|{primary_voice}")
        return _resolve_speaker(rng.choice(pool))
    return _resolve_speaker(pool[0])


async def tts_synthesize(
    text: str,
    *,
    target_language_code: str = "en-IN",
    speaker: str = "shubh",
    pace: float = 1.0,
) -> TTSResult:
    """Synthesize speech.

    LIVE: hits Bulbul v3 REST. Response carries base64 audio in `audios[0]`.
    MOCK: generates a soft chime tone WAV so the simulator can play it.

    Unknown voice names are mapped to known-good v3 voices automatically —
    a silent chime in production was the worst-case failure mode of this
    path before, and that's now impossible: the request always goes through
    with a valid speaker.
    """
    if settings.mock_mode or not text.strip():
        if not settings.allow_mock_providers:
            return TTSResult(
                audio_bytes=b"", mime="audio/wav", duration_s=0.0, mock=False,
                error="TTS unavailable: SARVAM_API_KEY is required and mock fallback is disabled",
            )
        return await _tts_mock_chime(seconds=2.5)

    speaker = _resolve_speaker(speaker)

    payload = {
        "text": text[:2500],   # REST char limit
        "target_language_code": target_language_code,
        "speaker": speaker,
        "model": "bulbul:v3",
        "pace": max(0.5, min(2.0, pace)),
    }
    headers = {"api-subscription-key": settings.sarvam_api_key,
               "Content-Type": "application/json"}
    url = f"{settings.sarvam_base_url}/text-to-speech"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), **httpx_client_kwargs()) as c:
            r = await c.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                # Surface the real error to the caller — invalid voice,
                # wrong model, etc. should not silently become a chime.
                body = r.text[:300]
                log.error("Sarvam TTS HTTP %d: %s | voice=%s lang=%s text_len=%d",
                          r.status_code, body, speaker,
                          target_language_code, len(text))
                if not settings.allow_mock_providers:
                    return TTSResult(
                        audio_bytes=b"", mime="audio/wav", duration_s=0.0, mock=False,
                        http_status=r.status_code, error=f"HTTP {r.status_code}: {body}",
                    )
                fallback = await _tts_mock_chime(seconds=1.5)
                fallback.mock = True
                fallback.http_status = r.status_code
                fallback.error = f"HTTP {r.status_code}: {body}"
                return fallback
            obj = r.json()
            audios = obj.get("audios") or []
            audio_b64 = audios[0] if audios else ""
            if not audio_b64:
                log.error("Sarvam TTS returned 200 but audios[0] is empty. "
                          "Response keys: %s", list(obj.keys()))
                err = f"audios[0] was empty. Keys: {list(obj.keys())}"
                if not settings.allow_mock_providers:
                    return TTSResult(audio_bytes=b"", mime="audio/wav", duration_s=0.0, mock=False, error=err)
                fallback = await _tts_mock_chime(seconds=1.5)
                fallback.mock = True
                fallback.error = err
                return fallback
            audio_bytes = base64.b64decode(audio_b64)
            log.info("Sarvam TTS OK: voice=%s lang=%s text_len=%d audio_bytes=%d",
                     speaker, target_language_code, len(text), len(audio_bytes))
            return TTSResult(audio_bytes=audio_bytes, mime="audio/wav",
                             duration_s=max(1.0, len(text) / 18.0), mock=False)
    except Exception as e:
        log.exception("Sarvam TTS call raised: %s", e)
        err = f"{e.__class__.__name__}: {str(e)[:200]}"
        if not settings.allow_mock_providers:
            return TTSResult(audio_bytes=b"", mime="audio/wav", duration_s=0.0, mock=False, error=err)
        fallback = await _tts_mock_chime(seconds=1.5)
        fallback.mock = True
        fallback.error = err
        return fallback


async def _tts_mock_chime(seconds: float = 2.0) -> TTSResult:
    """Generate a soft two-tone chime in WAV format (mock voice reply)."""
    rate = 22050
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        # Two notes, fading: 660Hz then 880Hz
        for i in range(n):
            t = i / rate
            note_a = math.sin(2 * math.pi * 660 * t)
            note_b = math.sin(2 * math.pi * 880 * t)
            mix = note_a if t < seconds * 0.45 else note_b
            envelope = max(0.0, 1.0 - abs(2 * (t / seconds) - 1.0))
            sample = int(0.3 * envelope * mix * 32767)
            frames.extend(struct.pack("<h", sample))
        w.writeframes(bytes(frames))
    return TTSResult(audio_bytes=buf.getvalue(), mime="audio/wav",
                     duration_s=seconds, mock=True)
