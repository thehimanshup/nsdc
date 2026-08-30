# NSDC Sovereign AI Substrate — PoC

Working proof-of-concept for **Part B (Sovereign AI Substrate)** of the NSDC
Skill India Digital Hub RFP (BID NO GEM/2026/B/7635371, §4.B). A governed
Graph-RAG platform for skilling: every answer cited to source evidence, every
access role-scoped, every action recorded in a signed audit chain — running on
the Sarvam model stack.

> **Substrate, not chatbot.** The RFP's hard guarantees (100% retrieval
> citations, hallucination thresholds, DPDP purpose-binding, complete
> auditability) are enforced once in the platform layer; every application
> surface inherits them.

## Quick start

```bat
run_substrate.bat            :: Windows — does everything (venv, deps, index, events, server)
./run_substrate.sh           #  Linux/macOS
```

Opens **http://localhost:8000/substrate-demo**. Provider comes from `.env`
`LLM_PROVIDER` (sarvam = live composition + groundedness judge; pass `mock`
as an argument for fully-offline demo mode).

| Demo login | Role | Sees |
|---|---|---|
| `meena / learner-demo` | learner | AI Skill Mentor (EN+HI, cited answers, honest refusals) |
| `rajesh / officer-demo` | officer (South Delhi) | Jurisdiction-scoped analytics, maker-checker draft notes |
| `iyer / sme-demo` | SME | QP/NOS coverage checks, Bloom-tagged item drafts |
| `admin / admin-demo` | admin | Audit trail (chain-verification badge), version registry |

Health checks: `python preflight_substrate.py` (what's enabled/missing) ·
`python -m pytest tests -q --ignore=tests/test_phase7_langchain_skills.py`
(50 tests) · `python -m evals.run_harness` (40-item gold-set evaluation).

## What works today

- **Grounded Graph-RAG pipeline**: hybrid retrieval (vector + BM25 + knowledge
  graph) with RBAC filtering *inside* the retrieval layer → evidence gate →
  LLM composition into a structured citation contract → synchronous citation
  hard-gate (uncited claims block) → response. Async LLM-judge scores
  groundedness on all traffic without touching latency.
- **Three personas live** on one substrate: Skill Mentor (learner), Officer
  Copilot (scoped analytics + maker-checker drafting), Content QA (SME).
- **Governance built-in**: Ed25519-signed hash-chained audit + consent
  ledgers, purpose-bound consent tokens, PII redaction, injection quarantine
  at ingestion, deterministic content-addressed index manifests (RFP 4.B.1c).
- **Measured, not promised** (gold-set baseline): citation completeness
  **100%**, refusal correctness **100%**, RBAC/injection refusal **100%**;
  hallucination rate measured live via the judge (`/api/v1/substrate/judge/stats`).

## Repository map

```
backend/substrate/       NEW substrate layer (the Part B core)
  schemas.py               fail-closed data contracts (citation contract, consent, events)
  ingest.py                register-driven corpus ingestion + injection quarantine
  manifest.py              deterministic index versioning (4.B.1c)
  retriever.py             hybrid 3-leg retrieval + RBAC predicate
  gates.py                 safety / evidence / citation / superlative gates
  composer.py              LLM citation-contract composer (+extractive fallback)
  service.py               the governed query pipeline
  judge.py                 async groundedness judge (KPI 7.2.2)
  analytics.py             jurisdiction-scoped officer analytics (model never writes SQL)
  drafts.py  contentqa.py  maker-checker notes · SME coverage/item tools
  authn.py  events.py      signed role tokens · synthetic event store
  kg/                      Neo4j ontology, curated seed, hash-versioned loader
backend/ (rest)           hardened base platform (Sarvam multi-LLM adapter, consent/audit,
                          PII redaction, voice: Saaras/Bulbul/LiveKit, WhatsApp, MCP client)
web/substrate.html        demo console (single file — kept as offline-safe fallback)
corpus/                   SOURCE_REGISTER.csv governs everything that gets ingested
evals/                    gold_v1.jsonl (40 items) + run_harness.py + results/
tests/                    50 tests incl. RBAC-adversarial + injection-quarantine suites
docs/proposal/            bid artifacts: blueprint, model card, SAICR, DPDP note,
                          eval report, compliance matrix, docx volume, pptx deck
docs/setup/               provider/voice/WhatsApp setup references
docker-compose.substrate.yml   Neo4j · Qdrant · Postgres · Jaeger
```

## Key documents

| Doc | Purpose |
|---|---|
| `design.md` | Architecture & design decisions (start here) |
| `DEMO_SCRIPT.md` | 20-minute presenter script + pre-flight + Q&A ammunition |
| `SUBSTRATE_POC_STATUS.md` | Full build history, session by session, with roadmap |
| `docs/proposal/01..06` | Bid collateral (blueprint → compliance matrix) |
| `../NSDC_Task_Plan_v1.xlsx` | Team execution plan (Sheet3 = master schedule) |
| `../PartB_PoC_PRD_v1.0.md` · `PartB_PoC_Feature_Stories_v1.0.md` | PRD + story backlog (ST-xxx IDs) |

## Operating rules

1. **One app process per data directory** — the signed audit chain assumes a
   single writer until the Kafka stream lands (Task Plan W4).
2. **Nothing enters the corpus without a register row** — ingestion fails
   closed on missing provenance/metadata (`corpus/SOURCE_REGISTER.csv`).
3. **No real learner data** — the event schema physically rejects
   non-`SYN-` learner IDs; all demo data is synthetic.
4. **Model/index/prompt changes go through SAICR** (`docs/proposal/03`):
   gold-set run attached; citations <100% or RBAC <100% = no ship.
5. The three substrate agents in `data/agents.json` carry grounding rules and
   role wiring the gates depend on — edit tone freely, treat
   `department_block`/`allowed_roles` as protected.

## Status

Live on Sarvam (sarvam-30b) with the groundedness judge active. Remaining
activation items and the 6-week team plan: see `SUBSTRATE_POC_STATUS.md`
(roadmap section) and the Task Plan workbook. Pre-award: internal use only
pending IP sign-off (ST-1306).
