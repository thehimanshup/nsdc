# Model Card & System Card — AI Skill Mentor (PoC)
### Per RFP §4.B.3: public model/system cards for every production model, adapter and assistant

**Card version:** 0.9 (PoC) · **System:** AI Skill Mentor on the Sovereign AI Substrate
**Date:** 2026-07-19 · **Owner:** Substrate PoC team

---

## System card

**Purpose.** Help learners discover NSQF-aligned courses, career pathways and
government skilling schemes, grounded exclusively in curated QP/NOS/scheme
evidence with per-claim citations.

**Users & roles.** Learners (public); Officer Copilot and Content QA variants
serve officer/SME roles under the same substrate with different scopes.

**Architecture.** Retrieval-grounded generation: hybrid retrieval (vector +
BM25 + knowledge-graph traversal) with RBAC filtering inside the retrieval
layer → evidence gate → LLM composition into a structured citation contract →
synchronous citation hard gate → response. Asynchronous LLM-judge scores
groundedness on all traffic. All interactions audit-logged (hash-chained,
Ed25519-signed) with consent tokens and index-manifest IDs.

**Models.**
| Component | PoC | Delivery |
|---|---|---|
| Generation | Sarvam-30B via API (mock/extractive fallback offline) | Sarvam 30B/105B + per-scheme LoRA adapters, vLLM, India-resident |
| Embeddings | BGE-M3 (open) or Sarvam embeddings | Sarvam embeddings (indigenous) |
| Judge | LLM-as-judge (separate model) | Independent judge + m-DeBERTa NLI cross-check |

**Data.** Public QP/NOS documents (NQR/HSSC), scheme guidelines (PMKVY 4.0,
NAPS, PM Vishwakarma, JSS, PM-DAKSH), NSQF descriptors, course metadata.
PoC uses clearly-marked SEED extracts pending official PDFs. **No real
learner data** — transactional analytics run on synthetic events with a
schema-level guard (learner IDs must be SYN- prefixed).

**Guardrails.**
- Refusals on insufficient evidence are decided by retrieval scores, before
  the model composes (no reliance on model honesty).
- 100% citation enforcement: uncited claims block the response (retry ×1,
  then withheld with a safe fallback).
- Cited IDs are validated against the retrieved evidence set (spoof-proof).
- Discriminatory-selection and PII-harvest requests refused and logged (EN+HI).
- Prompt-injection payloads quarantined at ingestion (restricted/admin-only);
  a permanent red-team plant document verifies this every run.
- The assistant recommends and explains only; it cannot enrol, pay, approve
  or submit anything. Officer drafts are maker-checker gated.

**Measured evaluation (gold set v1, 40 items, mock/BM25-only baseline, 2026-07-19).**
| Metric (RFP KPI) | Result |
|---|---|
| Citation completeness (7.2.4) | **100%** |
| Refusal correctness | **93.8%** |
| RBAC + injection refusal | **100%** |
| Retrieval must-cite hit rate | 92.3% |
| Latency p50 / p95 (7.2.1) | 15 / 39 ms (retrieval+gates; excludes live-LLM time) |
| Hallucination rate (7.2.2) | pending live judge (mock mode cannot score) |

**Known limitations (current PoC).**
1. Hindi retrieval degraded in BM25-only mode (6/40 gold failures) — resolved
   by the multilingual vector leg; measured per run until then.
2. Corpus is SEED extracts; all figures must be re-verified against official
   PDFs before any external demo claim.
3. Extractive fallback answers (offline mode) are verbatim-quote style — safe
   but less fluent than live-model composition.
4. Superlative queries ("best centre") answer from evidence rather than
   refusing — claim-level policy planned.
5. Single-writer audit log constraint until Kafka stream (delivery).

**Change control.** No model, adapter, prompt-contract or index change reaches
users without a SAICR review (doc 03): eval-harness run attached, KPI deltas
reviewed, rollback identified.

**Contact & disclosure.** security.txt/CVD intake per §4.B.7a at delivery;
AI-generated content is disclosed to users; officer drafts carry visible
AI-draft watermarks.
