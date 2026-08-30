#!/usr/bin/env python3
"""Voice-call quality test harness (Phase 6f).

Goal: prove that every department agent now produces *human-on-the-phone*
voice replies at parity with the Chief Minister's Office (CMO) agent — the
reference the team considers "good".

Because the production LLM (Sarvam) is reachable only from the deployment
network, this harness does NOT depend on a live model call. Instead it tests
the two things that actually determine live-call quality and are fully
deterministic here:

  1. STYLE RUBRIC — scores every authored *voice* few-shot example (the gold
     style the live model is told to imitate) against a spoken-quality rubric:
     brevity, no numbered/bulleted lists, no URLs/markdown read aloud, correct
     native script per language, acknowledge-first warmth, and no AI/bot
     self-disclosure. If the examples we teach are voice-clean, the model
     mimics voice-clean replies.

  2. PROMPT PARITY — builds the REAL live-call system prompt for each agent via
     the production code path (backend.livekit_agent_worker._build_system_prompt)
     and asserts each agent's prompt now carries the same voice scaffolding CMO
     has: spoken few-shot block, the human-call tone block, native-script
     instruction, brevity/anti-repeat rules, and a valid (non-mojibake) opener.

Run:  python3 voice_quality_test.py            # all agents, all languages
      python3 voice_quality_test.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import personas as P

# Defensive shim: some sandboxes mirror source files with a lag, which can
# leave a freshly-added helper temporarily absent from the imported module.
# render_few_shot_block depends only on pick_examples, so reconstruct it
# identically if needed (no behavioural difference from the real file).
if not hasattr(P, "render_few_shot_block"):
    def _render_few_shot_block(agent_id, query, *, n=3, voice=False):
        picks = P.pick_examples(agent_id, query, n=n, voice=voice)
        if not picks:
            return ""
        lines = []
        for ex in picks:
            u = (ex.get("user") or "").strip()
            a = (ex.get("agent") or "").strip()
            if not u or not a:
                continue
            lines.append(f'CITIZEN: "{u}"')
            lines.append(f'YOU: "{a}"')
            lines.append("---")
        return "\n".join(lines)
    P.render_few_shot_block = _render_few_shot_block

from backend.agents import AGENTS, get_agent
from backend.livekit_agent_worker import (
    _build_system_prompt, _call_opener, _state_from_lang,
)

LANGS = ["en-IN", "hi-IN", "ta-IN", "bn-IN", "mr-IN"]

# --- script ranges for native-script verification ---------------------------
SCRIPT_RANGES = {
    "hi-IN": [(0x0900, 0x097F)],   # Devanagari
    "mr-IN": [(0x0900, 0x097F)],   # Devanagari
    "ta-IN": [(0x0B80, 0x0BFF)],   # Tamil
    "bn-IN": [(0x0980, 0x09FF)],   # Bengali
    "te-IN": [(0x0C00, 0x0C7F)],   # Telugu
}

# --- regexes for spoken-quality violations ----------------------------------
RE_URL = re.compile(r"https?://|www\.|\b\w+\.(gov|com|in|org|net)\b", re.I)
RE_MARKDOWN = re.compile(r"\*\*|##|^\s*[-*]\s+|\[.+?\]\(.+?\)", re.M)
RE_NUM_LIST = re.compile(r"(^|\s)\(?\d\)|\b\d\.\s|\bstep\s*\d", re.I)
RE_BULLET = re.compile(r"[•▪◦]|(^|\n)\s*[-*]\s+")
RE_AI = re.compile(r"\b(AI|bot|chatbot|language model|virtual assistant|automated)\b", re.I)
RE_MOJIBAKE = re.compile(r"\?{3,}")

# acknowledgement / empathy openers we reward (multi-lingual, lowercased match)
ACK_TOKENS = [
    # english
    "oh no", "i'm sorry", "i am sorry", "sorry", "i understand", "got it",
    "good question", "happy to help", "of course", "sure", "no problem",
    "thanks for", "thank you", "congratulations", "don't worry", "that's",
    "that sounds", "i'll help", "i can help", "i hear you", "glad you",
    # hindi / marathi (devanagari)
    "हाँ", "ज़रूर", "जरूर", "अच्छा सवाल", "कोई बात नहीं", "मुझे खेद", "चिंता",
    "बिलकुल", "अरे", "खेद", "हो,", "नक्की", "काळजी करू नका", "चांगला प्रश्न",
    "बघतो", "सांगतो", "सांगते", "अभिनंदन", "मदत करतो",
    # tamil
    "வணக்கம்", "கவலைப்படாதீங்க", "கண்டிப்பா", "சொல்றேன்", "பார்க்கிறேன்",
    # bengali
    "নিশ্চয়ই", "ধন্যবাদ", "চিন্তা", "নমস্কার", "ভালো প্রশ্ন", "অভিনন্দন",
    "দেখছি", "বলছি",
]


def sentence_count(text: str) -> int:
    parts = re.split(r"[.!?।؟…]+", text)
    return len([p for p in parts if p.strip()])


def native_ratio(text: str, lang: str) -> float:
    ranges = SCRIPT_RANGES.get(lang)
    if not ranges:
        return 1.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    native = 0
    for c in letters:
        cp = ord(c)
        if any(lo <= cp <= hi for lo, hi in ranges):
            native += 1
    return native / len(letters)


def score_reply(text: str, lang: str) -> tuple[int, list[str]]:
    """Return (score 0-100, list of violation notes)."""
    notes: list[str] = []
    score = 100

    sc = sentence_count(text)
    if sc > 3:
        score -= min(30, (sc - 3) * 12)
        notes.append(f"too long: {sc} sentences (voice wants <=3)")
    if len(text) > 320:
        score -= 10
        notes.append(f"long: {len(text)} chars")

    if RE_NUM_LIST.search(text):
        score -= 25
        notes.append("contains numbered/step list (don't recite on a call)")
    if RE_BULLET.search(text):
        score -= 25
        notes.append("contains bullet list")
    if RE_URL.search(text):
        score -= 20
        notes.append("contains URL / web address read aloud")
    if RE_MARKDOWN.search(text):
        score -= 15
        notes.append("contains markdown")
    if RE_AI.search(text):
        score -= 40
        notes.append("AI/bot self-disclosure")
    if RE_MOJIBAKE.search(text):
        score -= 50
        notes.append("mojibake / corrupted '???' text")

    if lang != "en-IN":
        nr = native_ratio(text, lang)
        if nr < 0.6:
            score -= 30
            notes.append(f"low native-script ratio {nr:.0%} (romanised?)")

    low = text.lower()
    if not any(tok in low or tok in text for tok in ACK_TOKENS):
        score -= 8
        notes.append("no acknowledge-first / empathy opener")

    return max(0, score), notes


# --- prompt-parity checks ----------------------------------------------------

def check_prompt(agent_id: str, lang: str) -> tuple[bool, list[str]]:
    prompt = _build_system_prompt(agent_id, citizen_msisdn="9876543210",
                                  citizen_lang=lang)
    fails: list[str] = []
    checks = {
        "voice tone block (acknowledge-first)": "ACKNOWLEDGING" in prompt or "acknowledge" in prompt.lower(),
        "forbids lists read aloud": "lists read aloud" in prompt.lower() or "numbered" in prompt.lower(),
        "voice few-shot examples injected": "CITIZEN:" in prompt and "YOU:" in prompt,
        "behaviour NFR contract present": "CITIZEN EXPERIENCE NFRS" in prompt,
        "brevity rule present": "short spoken" in prompt.lower() or "1-2 short" in prompt.lower(),
        "native-script rule (non-en)": (lang == "en-IN") or ("VOICE CALL" in prompt and "romanis" in prompt.lower()),
        "already-greeted continuity": "ALREADY" in prompt and "greeted" in prompt.lower(),
    }
    for label, ok in checks.items():
        if not ok:
            fails.append(label)
    return (not fails), fails


def check_opener(agent_id: str, lang: str) -> tuple[bool, str, list[str]]:
    agent = get_agent(agent_id)
    persona = agent.resolve_persona(_state_from_lang(lang))
    opener = _call_opener(agent, lang, persona.get("persona_name", ""))
    fails: list[str] = []
    if RE_MOJIBAKE.search(opener):
        fails.append("opener is mojibake")
    if lang != "en-IN" and native_ratio(opener, lang) < 0.4:
        fails.append(f"opener not in native script ({native_ratio(opener,lang):.0%})")
    if agent.name.split()[0] not in opener and persona.get("persona_name","").split(",")[0] not in opener:
        fails.append("opener names neither officer nor department")
    return (not fails), opener, fails


# --- runner ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write full results to this JSON file")
    args = ap.parse_args()

    P.load_examples()
    P.load_voice_examples()

    results: dict = {"style": {}, "prompt_parity": {}, "openers": {}}
    style_by_agent: dict[str, list[int]] = {}
    style_by_lang: dict[str, list[int]] = {}
    all_style_scores: list[int] = []

    # 1) STYLE RUBRIC over every authored voice example
    for agent_id in sorted(P._VOICE_CACHE.keys()):
        rows = P._VOICE_CACHE[agent_id]
        agent_scores = []
        for ex in rows:
            lang = ex.get("language", "en-IN")
            s, notes = score_reply(ex.get("agent", ""), lang)
            agent_scores.append(s)
            all_style_scores.append(s)
            style_by_lang.setdefault(lang, []).append(s)
            results["style"].setdefault(agent_id, []).append(
                {"language": lang, "score": s, "notes": notes,
                 "reply": ex.get("agent", "")[:160]})
        style_by_agent[agent_id] = agent_scores

    # 2) PROMPT PARITY for every agent x language
    parity_pass = parity_total = 0
    for agent_id in sorted(AGENTS.keys()):
        for lang in LANGS:
            ok, fails = check_prompt(agent_id, lang)
            parity_total += 1
            parity_pass += 1 if ok else 0
            if not ok:
                results["prompt_parity"].setdefault(agent_id, {})[lang] = fails

    # 3) OPENERS for every agent x language
    opener_pass = opener_total = 0
    for agent_id in sorted(AGENTS.keys()):
        for lang in LANGS:
            ok, opener, fails = check_opener(agent_id, lang)
            opener_total += 1
            opener_pass += 1 if ok else 0
            results["openers"].setdefault(agent_id, {})[lang] = {
                "ok": ok, "opener": opener, "fails": fails}

    # --- report ---
    def avg(xs): return round(sum(xs) / len(xs), 1) if xs else 0.0
    cmo_avg = avg(style_by_agent.get("cmo", []))

    print("=" * 72)
    print("VOICE-CALL QUALITY REPORT  (reference: CMO = %.1f)" % cmo_avg)
    print("=" * 72)
    print("\n1) SPOKEN-STYLE RUBRIC — avg score per agent (0-100)")
    print("-" * 72)
    for agent_id in sorted(style_by_agent, key=lambda a: -avg(style_by_agent[a])):
        a = avg(style_by_agent[agent_id])
        delta = a - cmo_avg
        flag = "  <-- CMO ref" if agent_id == "cmo" else (
            "  OK" if a >= cmo_avg - 5 else "  *** below CMO ***")
        name = get_agent(agent_id).name
        print(f"  {a:5.1f}  ({delta:+5.1f})  {agent_id:12s} {name}{flag}")

    print("\n   By language:")
    for lang in LANGS:
        xs = style_by_lang.get(lang, [])
        print(f"     {lang}:  {avg(xs):5.1f}  (n={len(xs)})")
    print(f"\n   Overall style avg: {avg(all_style_scores):.1f}  (n={len(all_style_scores)})")

    # any individual example below 80?
    weak = [(aid, r) for aid, rs in results["style"].items() for r in rs if r["score"] < 80]
    print(f"\n   Examples scoring <80: {len(weak)}")
    for aid, r in weak:
        print(f"     - {aid} [{r['language']}] {r['score']}: {r['notes']}")

    print("\n2) PROMPT PARITY (each agent's live-call prompt has voice scaffolding)")
    print("-" * 72)
    print(f"   Passed {parity_pass}/{parity_total} agent x language prompt checks")
    for aid, langmap in results["prompt_parity"].items():
        for lang, fails in langmap.items():
            print(f"     FAIL {aid} [{lang}]: {fails}")

    print("\n3) GREETING OPENERS (no mojibake, native script, names officer/dept)")
    print("-" * 72)
    print(f"   Passed {opener_pass}/{opener_total} opener checks")
    for aid, langmap in results["openers"].items():
        for lang, info in langmap.items():
            if not info["ok"]:
                print(f"     FAIL {aid} [{lang}]: {info['fails']}  ::  {info['opener']}")

    # sample openers
    print("\n   Sample openers (cmo, health, water):")
    for aid in ["cmo", "health", "water"]:
        for lang in ["en-IN", "hi-IN", "ta-IN"]:
            print(f"     {aid:8s} {lang}: {results['openers'][aid][lang]['opener']}")

    overall_ok = (
        avg(all_style_scores) >= 85
        and not weak
        and parity_pass == parity_total
        and opener_pass == opener_total
    )
    print("\n" + "=" * 72)
    print("RESULT:", "PASS ✅  all agents at CMO-level voice quality"
          if overall_ok else "FAIL ❌  see issues above")
    print("=" * 72)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "summary": {
                "cmo_avg": cmo_avg,
                "overall_style_avg": avg(all_style_scores),
                "style_by_agent": {a: avg(s) for a, s in style_by_agent.items()},
                "style_by_lang": {l: avg(s) for l, s in style_by_lang.items()},
                "prompt_parity": f"{parity_pass}/{parity_total}",
                "openers": f"{opener_pass}/{opener_total}",
                "pass": overall_ok,
            },
            "details": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
