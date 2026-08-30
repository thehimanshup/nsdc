# Sovereign AI Substrate — Architecture Blueprint (Draft v0.9)
### RFP BID NO GEM/2026/B/7635371 · Part B §4.B · Q1 contractual deliverable draft

**Status:** pre-bid draft, evidenced by working PoC (`nsdc-substrate-poc`).
**Audience:** NSDC technical evaluation committee; our delivery architects.

---

## 1. Design thesis

Part B is a platform, not a chatbot. Our architecture treats §4.B.1 (data,
knowledge and intelligence) as the load-bearing substrate; the eleven AI
application surfaces of §4.B.4 are thin, role-scoped clients over it. Every
architectural guarantee the RFP demands — 100% retrieval citations (KPI 7.2.4),
<2% hallucination (7.2.2), DPDP purpose-binding, complete auditability
(4.B.11d) — is enforced in the substrate once, and inherited by every surface.

**This blueprint is not speculative.** A working thin vertical slice exists
today and is demoable offline: grounded Graph-RAG answers with per-claim
citations, role-scoped retrieval, jurisdiction-scoped analytics, maker-checker
drafting, signed audit chains, deterministic index versioning and a 40-item
evaluation harness reporting against the RFP KPI definitions. Measured
baseline: citation completeness 100%, refusal correctness 93.8%, RBAC/injection
refusal 100%.

## 2. Layer architecture (mapping to §4.B)

### Layer 2–3 — Data, Knowledge & Intelligence (§4.B.1)
- **Canonical Skilling Knowledge Graph** (4.B.1a): Neo4j; 13 node types
  (Sector→SSC→QP→NOS→Skill→Competency, JobRole, Course, TrainingProvider/
  Centre, Scheme, EligibilityRule, AssessmentItem); versioned releases with
  SHA-256 content hashes; SPARQL/Cypher/OpenAPI exposure. *PoC: ontology,
  loader and hash-versioned releases implemented; 3 golden-path healthcare
  QPs curated.*
- **Corpora** (4.B.1b): register-driven ingestion — no document enters the
  corpus without provenance (source_org, url, version, license, sensitivity,
  allowed roles/purposes); fail-closed metadata validation; injection-payload
  quarantine at ingestion. *PoC: implemented and red-team-planted.*
- **Vector & embedding stores** (4.B.1c): Qdrant with **in-store RBAC payload
  filtering** (role/purpose/sensitivity evaluated inside the store, before
  any content can reach model context); Sarvam embeddings primary, BGE-M3
  fallback; **deterministic index manifests** — every build content-addressed
  over {embedding model, chunking config, corpus snapshot, KG version}; every
  answer carries the manifest ID it was served from. *PoC: manifests live.*
- **Graph-RAG runtime** (4.B.1d): three-leg hybrid retrieval (vector + BM25 +
  KG traversal) fused by reciprocal-rank fusion; every generated assertion
  traceable to chunk IDs and KG node IDs. *PoC: live (BM25+KG legs; vector
  leg activates with infra).*
- **Governance, lineage & consent** (4.B.1e): Ed25519-signed, hash-chained
  consent ledger with purpose-bound, time-bound, revocable tokens attached to
  every downstream call; source-to-embedding lineage via manifests; DSR
  endpoints. *PoC: inherited from hardened base platform, re-verified.*
- **Transactional skilling-intelligence layer** (4.B.1f): canonical event
  schema (enrolment/attendance/assessment/certification/placement) with
  consent token per event; columnar/time-series store (ClickHouse at scale);
  feeds the feature store, predictive agents and SLM corpora. *PoC: schema +
  synthetic generator + scoped analytics live on SQLite (mechanical swap).*
- **Super-taxonomies** (4.B.1g): NSQF levels, Bloom levels, DGT trades and
  ESCO/O*NET crosswalks as first-class KG nodes; versioned + hashed. *PoC:
  crosswalk sample for healthcare job roles.*

### Layer 4 — Sovereign models (§4.B.2)
Provider-abstracted model layer, **Sarvam 30B/105B primary**, per-agent
override, quantised edge models (GGUF) for ITI/offline deployment, mock mode
for keyless environments. Fine-tuning roadmap: LoRA adapters per scheme/
sector on India-resident GPU (Q1 forking → Q3 first production adapters);
SSC/DGT/NCVET SLM family per §4.B.2.1 phased from Q3. All model changes gated
by SAICR (doc 03). *PoC: provider layer live incl. Sarvam client with
corporate-proxy hardening; composition runs on structured citation contracts
so model swaps don't change guarantees.*

### Safety & Responsible AI (§4.B.3) — cross-cutting
Gates in the request path, evaluation around it:
1. **Safety gate** (pre-retrieval): discriminatory-selection and PII-harvest
   refusals, bilingual patterns.
2. **Evidence gate** (pre-composition): refusals decided on retrieval scores,
   not model honesty.
3. **Citation hard gate** (post-composition, sync): every claim must cite;
   one retry then block. Anti-citation-spoofing: cited IDs validated against
   the actual evidence set.
4. **Groundedness judge** (async): LLM-judge entailment scoring on 100% of
   traffic — measures KPI 7.2.2 without touching P95 latency.
5. **Evaluation harness**: versioned gold set run per change; rbac/injection
   categories executed through the real authenticated API; user 👎 feedback
   auto-queues candidate eval items.

### Layer 5 — AI application surfaces (§4.B.4)
Thin clients inheriting substrate guarantees. PoC ships three:
AI Skill Mentor (learner; EN+HI; citations + KG pathway), Officer Copilot
(jurisdiction-scoped analytics, maker-checker draft notes), Content QA
(NOS-level coverage checks, Bloom-tagged item drafts, SME sign-off).
Remaining §4.B.4 surfaces (PASE, predictive dashboards, voice/IVR,
entrepreneurship companion, Skill Passport…) mount on the same contracts per
the Q3–Q6 milestone plan; voice stack (Saaras v3 ASR, Bulbul v3 TTS, LiveKit
WebRTC, WhatsApp) already present in the base platform.

### Layer 6 — Orchestration & trust (§4.B.5, §4.B.6)
Q5 scope: LangGraph-class runtime, DID-based agent identity with HSM-backed
signing, MCP/A2A interop (MCP client with India-only egress governance already
in base), Action Gateway with maker-checker + consent token per action —
the PoC's draft-note flow is the maker-checker pattern in miniature.
Credentials: W3C VC 2.0 / SD-JWT migration, did:web issuer registry,
BBS+ selective disclosure per §4.B.6 milestones.

## 3. Security & compliance posture (§4.B.7, §4.B.11)
Role/jurisdiction from verified tokens only (never client-supplied);
sensitivity clearance beats role listing (defence in depth); the analytics
engine never lets the model write SQL; all learner data in PoC is synthetic
(SYN- schema guard). Audit: hash-chained, Ed25519-signed, daily Merkle roots,
verify-chain surfaced in the operator console. Known constraint: file-based
audit assumes a single writer — delivery uses the RFP's Kafka audit stream.
India residency: AWS ap-south-1 / MeitY-empanelled equivalent; no cross-border
egress by default. Q1 hardening checklist (CVD/security.txt, TLS 1.3/HSTS,
S3 audit, .gov.in migration, CERT-In engagement, DPDP Phase 1, OWASP
ASVS/MASVS baselines) tracked in the delivery plan.

## 4. Observability & cost (§4.B.11b)
OpenTelemetry GenAI-semconv traces per request (retrieval spans, model spans
with token counts); per-surface cost metering; latency histograms against
KPI 7.2.1. Console exposes: audit browser with chain badge, eval dashboard
(KPI 7.2.x), version registry (index manifests, KG release hashes, model
versions).

## 5. Scale path
PoC (laptop, docker-compose) → pilot (single K8s cluster, Postgres/Qdrant/
Neo4j managed, vLLM inference) → production (ClickHouse events, Kafka audit,
HSM, multi-AZ Mumbai). Contracts (schemas, manifests, claims, consent tokens)
are identical at every scale — that is the point of the substrate.

---
*Companion documents: 02 model & system card · 03 SAICR process ·
04 DPDP consent & lineage design · 05 evaluation report (measured numbers).*
