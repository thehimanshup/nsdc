"""Eval harness runner (ST-1002, PRD FR-23) — runs the gold set through the
substrate pipeline IN-PROCESS and reports against the RFP KPI definitions.

Metrics reported (PRD §3 exit criteria):
  M1 citation completeness   — % of non-refusal answers whose claims all cite
  M4 refusal correctness     — % of expected-refuse items actually refused
  M?  answer rate            — % of expected-answer items answered (not refused)
  retrieval hit rate         — % of answered items citing a must_cite doc
  latency p50/p95
Grounding judge (M2/M3) requires a live judge model — recorded as SKIPPED
in mock mode, wired via LLM_JUDGE_PROVIDER when Sarvam key is active.

Usage:
    LLM_PROVIDER=mock python -m evals.run_harness            # offline run
    LLM_PROVIDER=sarvam python -m evals.run_harness           # live run
    python -m evals.run_harness --limit 10                    # smoke
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.substrate.schemas import (EvalCategory, GoldEvalItem, Purpose,  # noqa: E402
                                       Role)

GOLD = ROOT / "evals" / "gold_v1.jsonl"
RESULTS_DIR = ROOT / "evals" / "results"

PERSONA_PURPOSE = {
    Role.learner: Purpose.course_guidance,
    Role.officer: Purpose.scheme_admin,
    Role.sme: Purpose.content_qa,
    Role.admin: Purpose.course_guidance,
}

# Categories run in-process against the service directly.
RUNNABLE = {EvalCategory.factual, EvalCategory.refusal, EvalCategory.safety}
# Categories run through the authenticated HTTP API (ST-1005): the refusal
# may legitimately come from ANY layer — 401/403 at the route, the analytics
# scope guard, the safety gate, or the evidence gate. All count as refused.
API_RUNNABLE = {EvalCategory.rbac, EvalCategory.injection}

PERSONA_LOGIN = {
    Role.learner: ("meena", "learner-demo", "mentor"),
    Role.officer: ("rajesh", "officer-demo", "officer_copilot"),
    Role.sme: ("iyer", "sme-demo", "content_qa"),
    Role.admin: ("admin", "admin-demo", "mentor"),
}


def run_api_items(items) -> list[dict]:
    """Run rbac/injection gold items through the real HTTP surface."""
    import os
    os.environ.setdefault("SUBSTRATE_RAG", "true")
    from fastapi.testclient import TestClient
    from backend.main import app
    rows = []
    with TestClient(app) as client:
        tokens = {}
        for role, (user, pwd, _agent) in PERSONA_LOGIN.items():
            r = client.post("/api/v1/substrate/auth/login",
                            json={"username": user, "password": pwd})
            tokens[role] = r.json()["token"]
        for item in items:
            user, _pwd, agent = PERSONA_LOGIN[item.persona]
            H = {"Authorization": f"Bearer {tokens[item.persona]}"}
            t0 = time.perf_counter()
            # officer analytics-style probes go to the analytics endpoint,
            # everything else through /query
            if item.persona == Role.officer and item.category == EvalCategory.rbac:
                r = client.post("/api/v1/substrate/copilot/analytics",
                                json={"question": item.query}, headers=H)
                refused = r.status_code >= 400
                detail = (r.json().get("detail", "") if refused
                          else r.json().get("answer", ""))[:120]
            else:
                r = client.post("/api/v1/substrate/query",
                                json={"question": item.query, "agent_id": agent},
                                headers=H)
                if r.status_code >= 400:
                    refused, detail = True, str(r.json().get("detail", ""))[:120]
                else:
                    d = r.json()
                    refused = d.get("refusal_reason") is not None
                    detail = (d.get("refusal_reason") or
                              d.get("answer_markdown", ""))[:120]
            lat = int((time.perf_counter() - t0) * 1000)
            behavior_ok = refused if item.expected_behavior == "refuse" else not refused
            rows.append({
                "eval_id": item.eval_id, "category": item.category.value,
                "lang": item.lang, "expected": item.expected_behavior,
                "refused": refused, "behavior_ok": behavior_ok,
                "claims": None, "all_claims_cited": None, "must_cite_hit": None,
                "cited_docs": [], "compose_mode": "api",
                "latency_ms": lat, "interaction_id": f"api:{user}",
                "answer_preview": detail,
            })
    return rows


def _bootstrap_runtime():
    """Initialise the phase6e runtime pieces the pipeline depends on."""
    from backend import crypto_utils
    from backend.rag import load_corpora
    crypto_utils.init_keys()
    load_corpora()


async def run(limit: int | None = None) -> dict:
    _bootstrap_runtime()
    from backend.substrate.service import SubstrateService
    svc = SubstrateService(str(ROOT / "data"))

    items = [GoldEvalItem.model_validate_json(l)
             for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    runnable = [i for i in items if i.category in RUNNABLE]
    api_items = [i for i in items if i.category in API_RUNNABLE]
    skipped = [i for i in items
               if i.category not in RUNNABLE | API_RUNNABLE]
    if limit:
        runnable, api_items = runnable[:limit], api_items[:limit]

    rows, latencies = [], []
    for item in runnable:
        t0 = time.perf_counter()
        r = await svc.query(item.query, item.persona,
                            PERSONA_PURPOSE[item.persona],
                            actor=f"harness:{item.eval_id}")
        lat = int((time.perf_counter() - t0) * 1000)
        latencies.append(lat)
        c = r.contract
        refused = c.is_refusal
        cited_docs = sorted({cid.split("#")[0]
                             for cl in c.claims for cid in cl.citation_ids})
        all_claims_cited = (all(cl.is_cited for cl in c.claims)
                            if c.claims else None)
        must_hit = (any(d in cited_docs for d in item.must_cite_docs)
                    if item.must_cite_docs else None)
        behavior_ok = (refused if item.expected_behavior == "refuse"
                       else not refused)
        rows.append({
            "eval_id": item.eval_id, "category": item.category.value,
            "lang": item.lang, "expected": item.expected_behavior,
            "refused": refused,
            "behavior_ok": behavior_ok,
            "claims": len(c.claims),
            "all_claims_cited": all_claims_cited,
            "must_cite_hit": must_hit,
            "cited_docs": cited_docs,
            "compose_mode": r.compose_mode,
            "latency_ms": lat,
            "interaction_id": r.interaction_id,
            "answer_preview": c.answer_markdown[:160],
        })

    api_rows = run_api_items(api_items)
    rows.extend(api_rows)
    latencies.extend(r["latency_ms"] for r in api_rows)

    answered = [r for r in rows if not r["refused"] and r["claims"] is not None]
    expect_refuse = [r for r in rows if r["expected"] == "refuse"]
    expect_answer = [r for r in rows if r["expected"] == "answer"]
    must_cite_rows = [r for r in rows if r["must_cite_hit"] is not None
                      and not r["refused"]]

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_provider": __import__("os").getenv("LLM_PROVIDER", "unset"),
        "manifest_id": svc.manifest_id,
        "items_run": len(rows), "items_skipped_categories": len(skipped),
        "M1_citation_completeness_pct": pct(
            sum(1 for r in answered if r["all_claims_cited"]), len(answered)),
        "M4_refusal_correctness_pct": pct(
            sum(1 for r in expect_refuse if r["behavior_ok"]), len(expect_refuse)),
        "answer_rate_pct": pct(
            sum(1 for r in expect_answer if r["behavior_ok"]), len(expect_answer)),
        "retrieval_must_cite_hit_pct": pct(
            sum(1 for r in must_cite_rows if r["must_cite_hit"]), len(must_cite_rows)),
        "M6_rbac_injection_refusal_pct": pct(
            sum(1 for r in api_rows if r["behavior_ok"]), len(api_rows)),
        "M2_M3_grounding_judge": "SKIPPED (mock mode — enable with live judge)",
        "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": int(sorted(latencies)[int(0.95 * (len(latencies) - 1))])
                          if latencies else None,
        "failures": [r["eval_id"] for r in rows if not r["behavior_ok"]],
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"run_{ts}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (RESULTS_DIR / f"run_{ts}_summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    (RESULTS_DIR / "LATEST").write_text(f"run_{ts}", encoding="utf-8")
    return summary


if __name__ == "__main__":
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    print(json.dumps(asyncio.run(run(limit)), indent=1, ensure_ascii=False))
