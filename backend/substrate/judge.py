"""Async groundedness judge (ST-504, RFP KPI 7.2.2 / 4.B.11b).

Scores every non-refusal answer AFTER it has been returned to the user:
for each claim, is the claim text entailed by the text of its cited chunks?

Design constraints:
  - NEVER blocks the response path (latency KPI 7.2.1) — fire-and-forget
    task; failures are logged, not raised.
  - Judge model is a SEPARATE provider handle from the composer
    (LLM_JUDGE_PROVIDER env; defaults to the platform default) so the
    composer cannot grade its own homework at delivery.
  - In mock mode the judge records status="skipped" — hallucination rate
    is only ever reported from real judge runs, never fabricated.
  - Scores are persisted (data/judge_scores.jsonl) keyed by interaction_id
    so the console and harness can join them to audit records.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schemas import CitationContract

log = logging.getLogger("substrate.judge")

_LOCK = threading.Lock()

JUDGE_SYSTEM = (
    "You are a strict grounding auditor. For each CLAIM and its EVIDENCE, "
    "answer whether the claim is fully supported by the evidence text. "
    'Respond ONLY with a JSON list, one object per claim, in order. '
    'Each object has the form {"claim": <claim number>, "verdict": <VERDICT>} '
    "where <VERDICT> is exactly one of the three strings: "
    '"supported", "partial", or "unsupported". '
    'Example response for two claims: '
    '[{"claim": 1, "verdict": "supported"}, {"claim": 2, "verdict": "partial"}]. '
    "'supported' = every fact in the claim appears in the evidence. "
    "'partial' = some facts supported, others absent. "
    "'unsupported' = the evidence does not back the claim.")

VERDICT_SCORE = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}


def _parse_verdicts(raw: str) -> list[dict]:
    """Parse judge model output into a verdict list, tolerating the common
    real-world response shapes that the previous single-regex approach
    (`re.search(r"\\[.*\\]")` + json.loads) choked on — those produced the
    'judge returned no parseable verdicts' error records:
      - ```json fenced output``` (many models fence even with json_mode)
      - a wrapper object {"verdicts": [...]} or {"results": [...]}
      - a bare single object for a one-claim answer
      - trailing prose after the JSON
    Returns [] if nothing parseable is present (caller raises)."""
    text = raw.strip()
    # strip markdown fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()

    candidates = []
    # 1) whole-string parse
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        pass
    # 2) first JSON array embedded anywhere (non-greedy, then widen)
    if not candidates:
        for pattern in (r"\[[^\[\]]*\]", r"\[.*\]"):
            m = re.search(pattern, text, re.S)
            if m:
                try:
                    candidates.append(json.loads(m.group(0)))
                    break
                except json.JSONDecodeError:
                    continue
    # 3) single JSON object (one-claim answers)
    if not candidates:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                candidates.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                pass

    for c in candidates:
        if isinstance(c, list):
            return [v for v in c if isinstance(v, dict)]
        if isinstance(c, dict):
            for key in ("verdicts", "results", "claims"):
                if isinstance(c.get(key), list):
                    return [v for v in c[key] if isinstance(v, dict)]
            if "verdict" in c:      # single-claim bare object
                return [c]
    return []


def _scores_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "judge_scores.jsonl"


def _persist(data_dir, record: dict) -> None:
    with _LOCK:
        with _scores_path(data_dir).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _evidence_text(contract: CitationContract, chunks_by_id: dict) -> list[tuple[str, str]]:
    pairs = []
    for cl in contract.claims:
        ev = " ".join(chunks_by_id.get(cid, "") for cid in cl.citation_ids)
        pairs.append((cl.text, ev[:1500]))
    return pairs


async def judge_contract(interaction_id: str, contract: CitationContract,
                         chunks_by_id: dict[str, str],
                         data_dir: str | Path = "data") -> Optional[dict]:
    """Score one interaction. Returns the record (also persisted), or None."""
    base = {
        "interaction_id": interaction_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claims": len(contract.claims),
    }
    if contract.is_refusal or not contract.claims:
        return None  # nothing to grade

    try:
        from ..llm import get_llm_for
        provider = os.getenv("LLM_JUDGE_PROVIDER") or None
        llm = get_llm_for(provider)
        if getattr(llm, "mock_mode", False):
            record = {**base, "status": "skipped",
                      "reason": "mock provider — hallucination rate not scorable"}
            _persist(data_dir, record)
            return record

        pairs = _evidence_text(contract, chunks_by_id)
        prompt = "\n\n".join(
            f"CLAIM {i+1}: {c}\nEVIDENCE {i+1}: {e or '(no evidence text found)'}"
            for i, (c, e) in enumerate(pairs))
        raw = await llm.chat_complete(
            messages=[{"role": "system", "content": JUDGE_SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=300, json_mode=True)
        verdicts = _parse_verdicts(raw)
        # Fail-closed measurement: a verdict value outside the three valid
        # strings (e.g. the model echoing a template placeholder) must be
        # treated as an invalid judge run — an ERROR record — never scored.
        # This is exactly what produced the phantom "55.6% hallucination
        # rate": echoed placeholders fell through .get(..., 0.0) and were
        # counted as unsupported claims.
        invalid = [str(v.get("verdict", "")) for v in verdicts
                   if str(v.get("verdict", "")).lower() not in VERDICT_SCORE]
        if invalid:
            raise ValueError(f"judge returned invalid verdict value(s): {invalid[:3]}")
        scores = [VERDICT_SCORE[str(v.get("verdict", "")).lower()]
                  for v in verdicts][:len(contract.claims)]
        if not scores:
            raise ValueError("judge returned no parseable verdicts")
        record = {**base, "status": "scored",
                  "judge_provider": provider or "default",
                  "verdicts": verdicts[:len(contract.claims)],
                  "groundedness": round(sum(scores) / len(scores), 3),
                  "unsupported_claims": sum(1 for s in scores if s == 0.0)}
    except Exception as e:
        record = {**base, "status": "error", "reason": str(e)[:200]}
        log.warning("judge failed for %s: %s", interaction_id, e)
    _persist(data_dir, record)
    return record


def fire_and_forget(interaction_id: str, contract: CitationContract,
                    chunks_by_id: dict[str, str], data_dir: str | Path = "data") -> None:
    """Schedule judging without awaiting it. Safe to call from the request path."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(judge_contract(interaction_id, contract, chunks_by_id, data_dir))
    except RuntimeError:  # no running loop (sync tests) — run inline
        asyncio.run(judge_contract(interaction_id, contract, chunks_by_id, data_dir))


def stats(data_dir: str | Path = "data") -> dict:
    """Aggregate judge results — the KPI 7.2.2 measurement surface."""
    p = _scores_path(data_dir)
    if not p.exists():
        return {"scored": 0, "skipped": 0, "errors": 0, "note": "no judge runs yet"}
    scored, skipped, errors, ground, unsupported, claims = 0, 0, 0, [], 0, 0
    for line in p.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["status"] == "scored":
            scored += 1
            ground.append(r["groundedness"])
            unsupported += r.get("unsupported_claims", 0)
            claims += r.get("claims", 0)
        elif r["status"] == "skipped":
            skipped += 1
        else:
            errors += 1
    out = {"scored": scored, "skipped": skipped, "errors": errors}
    if scored:
        out["mean_groundedness"] = round(sum(ground) / len(ground), 3)
        out["hallucination_rate_pct"] = round(100.0 * unsupported / max(claims, 1), 2)
        out["claims_scored"] = claims          # sample size behind the rate
        out["kpi"] = "7.2.2"
    else:
        out["note"] = "hallucination rate reported only from real judge runs"
    return out
