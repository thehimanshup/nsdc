# Evaluation Report — Sovereign AI Substrate PoC (Run baseline v1)
### Methodology + measured results against RFP KPI §7.2.x · 2026-07-19

## 1. Methodology
- **Eval-first:** the gold set was authored before the pipeline was built and
  is versioned in git (`evals/gold_v1.jsonl`).
- **Gold set v1: 40 items** — 23 factual (15 EN / 8 HI), 6 out-of-corpus
  refusal, 5 RBAC adversarial, 3 injection, 3 safety. Every factual item
  declares `must_cite_docs`; every item declares expected behaviour
  (answer/refuse) and rubric notes.
- **Execution:** factual/refusal/safety run in-process against the substrate
  service; **RBAC and injection items run through the real authenticated HTTP
  API** with per-persona tokens — a refusal from any legitimate layer (401/403,
  jurisdiction scope guard, safety gate, evidence gate) counts.
- **Harness:** one command (`python -m evals.run_harness`); results JSONL +
  summary persisted per run (`evals/results/`); designed for nightly CI.
- **Environment for this baseline:** mock LLM (extractive composition),
  BM25-only retrieval, SEED corpus (10 docs → 40 chunks, 2 quarantined),
  index manifest `man-b30ca1faa4c9`. This is the *floor* configuration —
  fully offline, zero external dependencies.

## 2. Results (baseline v1)

| Metric | RFP KPI | Target | Measured |
|---|---|---|---|
| Citation completeness | 7.2.4 | 100% | **100%** |
| Refusal correctness (expected-refuse items) | — | ≥90% (PRD M4) | **93.8%** |
| RBAC + injection refusal | — | 100% (PRD M6) | **100%** |
| Retrieval must-cite hit rate | — | — | **92.3%** |
| Latency p50 / p95 | 7.2.1 | <1s (delivery) | **15 / 39 ms** (pipeline w/o live LLM) |
| Hallucination / groundedness | 7.2.2 | <2% (delivery) | **not scorable in mock** — judge activates with live model |
| Answer rate (expected-answer items) | — | — | 54.2% (see §3) |

## 3. Failure analysis (12/40) — every failure is attributed and tracked
| Class | Count | Cause | Fix & owner |
|---|---|---|---|
| Hindi cross-lingual retrieval | 6 | BM25 cannot match HI queries to EN corpus | Multilingual vector leg (BGE-M3/Sarvam embeddings) — activates with docker stack; measured per run until closed |
| EN recall misses | 4 | Keyword-only retrieval + strict coverage-based evidence gate on tiny SEED corpus | Vector leg + real corpus; gate thresholds re-tuned after |
| Superlative query answered ("best centre") | 1 | No claim-level policy for comparative/superlative asks | Claim-policy check planned (Sprint 3 hardening) |
| Emotional-support query refused | 1 | Substrate QA path is evidence-gated by design; empathetic queries belong to the conversational path | Route by intent to orchestrator path |

**Reading the 54.2% answer rate correctly:** the system currently prefers
refusing over risking an ungrounded answer — the failure mode is conservative,
never fabrication. Zero fabricated entities were observed across all runs;
citation completeness held at 100% throughout.

## 4. Defence-in-depth results (adversarial)
- Token tampering (role-flip forgery): rejected (signature check).
- Prompt-based jurisdiction widening ("show ALL districts"): no effect —
  scope injected from verified claims, not model output.
- PII harvest (Aadhaar/phone/bank lists): refused + logged in both the
  analytics engine and the query safety gate (EN+HI patterns).
- Injection plant: 2/3 chunks of the planted document auto-quarantined at
  ingestion (restricted/admin-only); leak test proves no role/purpose
  combination retrieves them; benign chunk serves normally.
- Discriminatory-selection requests: refused with neutral eligibility-based
  alternative (EN+HI).
- Audit tamper: modifying any past record breaks the hash chain, surfaced as
  a live badge in the operator console. (During development, concurrent
  processes forked the chain — detected by verification exactly as designed;
  single-writer constraint documented, Kafka stream at delivery.)

## 5. Next measurement milestones
1. **Live-model run** (Sarvam key): activates the async groundedness judge →
   first hallucination-rate measurement (KPI 7.2.2); extractive→synthesised
   composition quality comparison.
2. **Vector-leg run** (docker + embeddings): expected to clear the 10
   Hindi/recall failures; Hindi grounding tracked against PRD M8 (≥75%).
3. **Real-corpus run** (official QP/scheme PDFs): re-verify every SEED-based
   gold answer; expand gold set to 80 items incl. SME-reviewed rubrics.
4. **Judge calibration:** ≥30-sample SME agreement check (target <15%
   disagreement) before hallucination numbers are quoted externally.
