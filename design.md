# Design — NSDC Sovereign AI Substrate PoC

Architecture and design decisions for the Part B (§4.B) substrate.
Companion to `README.md` (orientation) and `docs/proposal/01_architecture_blueprint.md`
(the full RFP-facing blueprint). This document explains **how the code works
and why it is shaped this way**.

## 1. Design thesis

Part B's eleven AI application surfaces share four non-negotiable guarantees:
100% retrieval citations (KPI 7.2.4), bounded hallucination (7.2.2), DPDP
purpose-bound access, and complete auditability (4.B.11d). We enforce each
guarantee **once, in the substrate**, below the application layer:

- Citations are a **data contract**, not a prompt instruction.
- Refusals are decided by **retrieval evidence**, not model honesty.
- Access control runs **inside retrieval**, before content can reach a prompt.
- Audit is **cryptographic** (signed hash chain), not log files.

Surfaces (Mentor, Copilot, Content QA — and later PASE, dashboards, agents)
are thin clients; adding one adds no new trust surface.

## 2. Request path

```
question
  → greeting short-circuit (small talk never hits retrieval)
  → SAFETY GATE          discriminatory-selection, PII-harvest, authority
                         (certify/guarantee) refusals — EN+HI patterns
  → HYBRID RETRIEVAL     vector (Qdrant, RBAC-filtered IN-STORE)
                         + BM25 (phase6e index, focus-pass rewrite + RBAC post-filter)
                         + KG traversal (Neo4j pathway queries, `pathway` intent)
                         → reciprocal-rank fusion → EvidenceBundle
  → EVIDENCE GATE        top-score & coverage thresholds; superlative gate
                         (comparative asks need comparative numeric evidence)
                         → grounded refusal with nearest alternative
  → COMPOSER             LLM → CitationContract {answer, claims[{text,
                         citation_ids, kg_node_ids}], confidence};
                         cited IDs validated against the evidence set
                         (anti-spoofing); deterministic EXTRACTIVE fallback
                         when no live model (offline demo mode)
  → CITATION HARD GATE   any uncited claim → one retry → blocked with safe
                         fallback. Synchronous, cheap — protects KPI 7.2.4.
  → AUDIT EVENT          actor, role, purpose, consent token, retrieved chunk
                         IDs, KG nodes, manifest ID, gate results, latency
  → response             (then, async) GROUNDEDNESS JUDGE — separate model
                         scores claim-vs-evidence entailment on 100% of
                         traffic; never blocks; “skipped” in mock mode so the
                         hallucination number is never fabricated
```

Code: `backend/substrate/service.py` orchestrates; each stage lives in its
own module (`gates.py`, `retriever.py`, `composer.py`, `judge.py`).

## 3. Data layer decisions

**Register-driven corpus (4.B.1b).** `corpus/SOURCE_REGISTER.csv` is the
source of truth: no register row → no ingestion. Every document carries
provenance (org, url, version, license), sensitivity, `allowed_roles[]` and
`allowed_purposes[]`; chunks inherit these so RBAC needs no join at query
time. Validation fails closed.

**Injection quarantine at ingestion (4.B.3).** Chunks containing
instruction-like payloads are forced to `restricted`/admin-only before they
can ever enter a prompt. A red-team plant document lives permanently in the
corpus; tests prove no role/purpose combination retrieves its payload chunks.

**Deterministic manifests (4.B.1c).** Every index build writes a
content-addressed manifest `{embedding_model, chunking_config_hash,
corpus_snapshot_hash, kg_version}`; every answer records the manifest it was
served from. Identical inputs → identical manifest ID (rebuild verification).

**Knowledge graph (4.B.1a/1g).** Neo4j; 13 node types
(Sector→SSC→QP→NOS→Skill→Competency, JobRole, Course, Provider/Centre,
Scheme, EligibilityRule, AssessmentItem) + crosswalk nodes (NSQFLevel,
BloomLevel, ExternalOccupation for ESCO/O*NET). Loads are hash-versioned
releases (`data/kg_releases/`). Machine-readable `EligibilityRule` nodes make
scheme eligibility deterministic rather than generative.

**Transactional layer (4.B.1f).** Canonical `SkillingEvent` schema
(enrolment/attendance/assessment/certification/placement) with a consent
token per event and a schema-level guard rejecting non-synthetic learner IDs
in the PoC. SQLite now; the interface is designed for the
Postgres→ClickHouse swap.

## 4. Access control model

Identity: HMAC-signed tokens (Keycloak-shaped claims: `{sub, role,
jurisdiction, exp}`) — the claim shape is the contract that survives the
Keycloak swap. Three enforcement points, defence in depth:

1. **Route**: per-agent role allow-list (learner→mentor, officer→copilot,
   sme→content_qa); 401/403 audited.
2. **Retrieval**: single RBAC predicate (`role ∈ allowed_roles ∧ purpose ∈
   allowed_purposes ∧ sensitivity ≤ clearance`) applied in-store for the
   vector leg and post-hoc for BM25. Sensitivity clearance overrides role
   listing (a mislabeled chunk still cannot leak).
3. **Analytics**: natural language maps to **vetted SQL templates only** —
   the model never writes SQL; jurisdiction WHERE-clauses are injected from
   verified token claims, so prompt content cannot widen scope. PII-harvest
   patterns are refused before template matching.

## 5. Model layer

Provider abstraction (`backend/llm/`): Sarvam 30B primary (per-agent
override), Ollama for edge/offline, mock for zero-egress demos. The composer
consumes any provider because the citation contract, not the model, carries
the guarantees. The judge uses a **separate provider handle**
(`LLM_JUDGE_PROVIDER`) so the composer never grades its own work.
Fine-tuning (LoRA per scheme/sector, RFP 4.B.2) plugs in behind the same
contract — see Task Plan gap row "Fine-Tuning Readiness".

## 6. Governance

- **Audit**: append-only JSONL, Ed25519-signed, hash-chained, daily Merkle
  roots; `verify_chain()` surfaced as a live badge in the console. Constraint:
  single writer per data dir until the Kafka stream (Task Plan W4).
- **Consent**: purpose-bound, time-bound, revocable tokens recorded in a
  signed ledger; every downstream call and audit record carries the token.
- **SAICR** (4.B.11d): every model/index/prompt/gate change requires a
  gold-set run; hard blocks at citations <100% or RBAC/injection <100%.
  Process: `docs/proposal/03_saicr_process.md`.

## 7. Evaluation design (eval-first)

The gold set (`evals/gold_v1.jsonl`, 40 items: factual EN/HI, refusal, RBAC,
injection, safety) was written **before** the pipeline was built and is
versioned in git. `evals/run_harness.py` runs factual categories in-process
and adversarial categories **through the authenticated HTTP API** with
per-persona tokens — a refusal from any legitimate layer counts. Results are
persisted per run; user 👎 feedback auto-queues candidate items
(`evals/gold_candidates.jsonl`). The judge provides the KPI 7.2.2 measurement
surface (`/api/v1/substrate/judge/stats`).

## 8. Inherited platform (reuse boundary)

The substrate extends a hardened multi-agent platform (phase6e lineage):
Sarvam client with proxy hardening, consent/audit cryptography, PII
redaction, DSR endpoints, prompt-safety fencing, voice stack (Saaras v3 ASR,
Bulbul v3 TTS, LiveKit), WhatsApp via Twilio, MCP client with India-only
egress. Reuse rule: substrate modules **import** platform modules; they do
not modify them. Channel integration (WhatsApp/voice) must route through
`substrate.service.query()` so channels inherit the gates (Task Plan W4–W5).

## 9. Scale path

| Concern | PoC | Delivery |
|---|---|---|
| Auth | HMAC demo tokens | Keycloak OIDC (same claims) |
| Stores | SQLite/JSON | Postgres → ClickHouse (events) |
| Audit | file chain (single writer) | Kafka stream, same signing |
| Inference | Sarvam API | vLLM, India-resident GPU, LoRA adapters |
| Deploy | docker-compose, 1 box | K8s multi-AZ (ap-south-1) |

Contracts (schemas, manifests, claims, consent tokens, citation contract)
are identical at every scale — that is the substrate argument.

## 10. Known limitations (tracked)

Single-writer audit until Kafka · Hindi retrieval depends on the multilingual
vector leg (BM25 is not cross-lingual) · emotional-support queries are
evidence-gated (belong on a conversational path — routing planned) ·
extractive fallback answers are verbatim-quote style (safe, less fluent) ·
demo users hardcoded until the user-admin UI (Task Plan W4).
