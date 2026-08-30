# nsdc-substrate-poc — Build Status
Updated: 2026-07-19 · Baseline: phase6e fork · Stories: PartB_PoC_Feature_Stories_v1.0.md

## Done (Sprint 0 + early Sprint 1 scaffolds)
| Story | What landed | Verify |
|---|---|---|
| ST-101 | Legacy data purged (audit/audio/uploads/consent/citizen stores, old keys); .gitignore added; gov-services docs & pptx removed. `.env` kept for dev — **ROTATE the Sarvam key before sharing** | `ls data/` |
| ST-102 | Callback platform removed (8 modules + routes + tests); main.py, routes_admin, routes_twilio, skills.py patched; voice/WhatsApp kept | `grep -r callback_ backend/` |
| ST-103 | 3 skilling agents replace 11 dept agents (data/agents.json): mentor, officer_copilot, content_qa — grounding + citation + maker-checker rules in prompts | `GET /api/v1/agents` |
| ST-103b | data/schemes.json re-seeded: PMKVY 4.0, NAPS, PM Vishwakarma, JSS, PM-DAKSH with machine-readable eligibility; FAMILIES updated | `GET /api/v1/schemes` |
| ST-104 | Mock-mode boot verified end-to-end: auth → mentor chat → skilling reply with citation markers. 21 tests pass | `LLM_PROVIDER=mock uvicorn backend.main:app` |
| ST-202/501 | backend/substrate/schemas.py — DocumentMeta, ChunkPayload, CitationContract (claims+citations), SkillingEvent (SYN- guard), ConsentToken, GoldEvalItem. Fail-closed validators | tests/test_substrate_core.py |
| ST-204 | backend/substrate/manifest.py — deterministic, content-addressed index manifests + registry (RFP 4.B.1c) | tests pass |
| ST-502/503 | backend/substrate/gates.py — evidence gate (pre-composer refusal), citation hard gate (retry→block), bilingual refusal contracts | tests pass |
| ST-301 | docker-compose.substrate.yml (Neo4j 5.26, Qdrant 1.12, Postgres 16, Jaeger) + kg/bootstrap.cypher (13 node types, crosswalk refs, NSQF/Bloom seeds) | `docker compose -f docker-compose.substrate.yml up -d` |
| ST-302 seed | kg/curated/*.csv — 3 golden-path QPs (GDA, HHA, Phlebotomy), 8 NOS, skills, job roles w/ ESCO xwalk, courses, 4 SYN centres, PMKVY rules + kg/loader.py w/ versioned hashed releases | `python backend/substrate/kg/loader.py` (needs Neo4j) |
| ST-401/402 | backend/substrate/vector_store.py — Qdrant adapter, RBAC filter IN-STORE, Sarvam/BGE-M3 pluggable embedders | needs Qdrant + model |
| ST-403 | backend/substrate/retriever.py — hybrid RRF fusion (vector + phase6e BM25 + KG pathway leg), single RBAC predicate | wiring pending |
| ST-1001 | evals/gold_v1.jsonl — 40 schema-validated items (23 factual EN/HI, 6 refusal, 5 rbac, 3 injection, 3 safety) | `wc -l evals/gold_v1.jsonl` |
| ST-201 | corpus/SOURCE_REGISTER.csv (11 docs, golden-path QPs flagged) + governance README | `cat corpus/SOURCE_REGISTER.csv` |

## Next (human/team actions)
1. **ROTATE Sarvam API key** (was present in copied .env) — then run key/quota validation (ST-105).
2. Download the 11 registered public docs into corpus/raw/ (ST-201 completion).
3. `docker compose -f docker-compose.substrate.yml up -d` → run kg/loader.py → verify PATHWAY_QUERY returns the GDA path (ST-305 AC).
4. Wire retriever into orchestrator behind a `SUBSTRATE_RAG=true` flag (ST-404) — next build session.
5. IP/legal sign-off for phase6e reuse in the NSDC bid (ST-1306 — blocker for sharing).

## Verified test/QC state
- tests/test_substrate_core.py: 12/12 pass (schemas, manifest determinism, both gates)
- tests (agent config, orchestrator turn): 21/21 total pass after re-domaining
- App boots clean in mock mode; /api/v1/agents, /api/v1/schemes, mentor chat verified

---
## Session 2 — Grounded pipeline live (2026-07-19, later)

### New this session
| Story | What landed | Verify |
|---|---|---|
| ST-203 | `backend/substrate/ingest.py` — register-driven ingestion (md/txt/html/pdf), section-aware chunking, fail-closed metadata, BM25 export, optional --qdrant. 9 seed docs → 37 chunks, manifest `man-84f422d28af5` | `python -m backend.substrate.ingest` |
| Seed corpus | 9 SEED-marked docs in corpus/raw (3 QPs, PMKVY, NSQF, 2 courses, centres[SYNTHETIC], FAQ) — replace with official PDFs and re-ingest | `ls corpus/raw` |
| ST-501 | `composer.py` — LLM citation-contract composer + anti-citation-spoofing parse + deterministic EXTRACTIVE fallback (works in mock/offline) | — |
| ST-404 | `service.py` + `routes_substrate.py` — full pipeline behind `SUBSTRATE_RAG=true`: safety gate → hybrid retrieve (RBAC) → evidence gate → compose → citation hard gate → audit event. `/api/v1/substrate/{query,registry,health}`. Degrades gracefully: BM25-only without Qdrant/Neo4j | curl /api/v1/substrate/query |
| ST-521 | Safety pre-gate: discriminatory-selection requests (EN+HI patterns) → unsafe_request refusal with neutral eligibility-based alternative | gold G-038 passes |
| ST-1002 | `evals/run_harness.py` — in-process gold-set runner, writes results JSONL + summary, KPI-mapped metrics | `LLM_PROVIDER=mock python -m evals.run_harness` |

### First measured baseline (mock LLM, BM25-only, seed corpus)
- M1 citation completeness: **100%** · M4 refusal correctness: **87.5%**
- retrieval must-cite hit: **92.3%** · latency p50/p95: **13/16 ms**
- answer rate: 54.2% — dominated by known gaps below

### Known gaps the harness correctly surfaces (next build targets)
1. **Hindi retrieval (6 failures)** — BM25 can't match HI queries to EN corpus.
   Fix = vector leg with BGE-M3/Sarvam embeddings (ST-401 live run). This is
   exactly PRD Risk #2; the harness now measures it per run.
2. 4 EN recall misses — improve with vector leg + real (non-seed) corpus.
3. G-026 superlative ("best centre") answered — needs claim-level check.
4. G-040 emotional-support item refused by evidence gate — route such
   queries to the conversational path, not the substrate QA path.
5. rbac/injection gold categories (8 items) skipped — need auth path +
   planted docs wiring (ST-1004/1005).

### Runbook (offline demo, works today)
```
SUBSTRATE_RAG=true LLM_PROVIDER=mock uvicorn backend.main:app --port 8000
curl -X POST localhost:8000/api/v1/substrate/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"I passed class 10 and want a job in healthcare. Which course should I take?","role":"learner"}'
```
With a rotated Sarvam key: set LLM_PROVIDER=sarvam for synthesised (non-
extractive) contract answers; start docker-compose.substrate.yml + rerun
ingest --qdrant for the vector leg; run kg/loader.py for the KG leg.

---
## Session 3 — Auth, events & officer analytics (2026-07-19, later)

| Story | What landed | Verify |
|---|---|---|
| ST-601 | `substrate/authn.py` — HMAC-signed role tokens (stdlib; Keycloak-swappable claim shape: sub/role/jurisdiction/exp). 5 demo users: meena(learner), rajesh(officer, South Delhi), leela(officer, North West Delhi), iyer(sme), admin. Role now comes ONLY from the verified token — the trust-the-request-body hole is closed. `/query` requires Bearer; per-agent role allow-list (learner→mentor, officer→copilot, sme→content_qa); 401/403 with audit | tests + TestClient smoke |
| ST-701 | `substrate/events.py` — deterministic synthetic generator: 9,668 events (150 SYN- learners, 4 centres, 2 districts) with planted anomalies: TC-DEL-002 attendance sags to ~62% in the final month; TC-DEL-003 dropout ~25% | `python -m backend.substrate.events` |
| ST-702 | `substrate/analytics.py` + `/copilot/analytics` — template-based NL analytics (attendance incl. recent-30d deterioration, dropout, enrolment, certification funnel, placement). Jurisdiction WHERE injected from token claims — the model never writes SQL; unmatched questions refused. Answers carry the executed SQL + row data (reproducible figures). All attempts audited incl. refusals | demo query below |
| ST-1004 | `tests/test_substrate_rbac.py` — 12 adversarial tests: token tamper (role-flip forgery) rejected, prompt-based scope widening has no effect, learner analytics refused, purpose binding, restricted-chunk defence-in-depth (sensitivity clearance beats allowed_roles), free-form PII query refused | 12/12 pass |

**Demo scene now live (US-2.1):** rajesh logs in → "which centres have low attendance?" →
"TC-DEL-002 (South Delhi): overall 78%, last-30d 61% ⚠ DETERIORATING" — scoped to his
district only; the same question from leela shows only North West Delhi; scope-widening
prompts are ignored; every attempt lands in the signed audit log.

**Test state: 33/33 passing** (12 substrate core + 12 RBAC + 9 phase6e).

### Demo credentials (PoC only — rotate before any shared deployment)
meena/learner-demo · rajesh/officer-demo · leela/officer-demo-2 · iyer/sme-demo · admin/admin-demo

### Remaining next actions
1. Human unlocks unchanged: rotate Sarvam key · real QP PDFs → corpus/raw · docker stack + ingest --qdrant (fixes the 6 Hindi gold failures) · kg/loader.py · IP sign-off (ST-1306).
2. Build next: draft-note maker-checker endpoint (ST-703) · demo UI panel for citations/audit (ST-801/1101) · wire rbac/injection gold categories through TestClient into the harness (ST-1005) · gold-set expansion to 80 (ST-1001b).

---
## Session 4 — Maker-checker, demo console & full gold-set coverage (2026-07-19, later)

| Story | What landed | Verify |
|---|---|---|
| ST-703 | `substrate/drafts.py` + `/copilot/draft-note*` — scheme-status DRAFT notes compiled from the officer's OWN scoped analytics (enrolment, funnel, attendance watchlist, dropout, placement), watermarked, pending→approved/rejected with human decision only. Decided drafts immutable (409); cross-officer decisions denied (403); all transitions audited | TestClient smoke |
| ST-801/1101 | **`/substrate-demo`** — single-file demo console: role-aware login (5 demo users), grounded chat with per-claim citation chips + gate pass/stop badges + manifest id, scoped analytics with reproducible-SQL disclosure, draft review with approve/reject, audit trail with live chain-verification badge, version registry (manifests + KG releases) | open http://localhost:8000/substrate-demo |
| ST-1005 | Harness now runs **all 40 gold items** — rbac+injection categories execute through the real authenticated HTTP surface with per-persona tokens; refusal from any layer (401/403, scope guard, safety gate, evidence gate) counts | harness run |
| Fixes | PII-harvest screen in both analytics AND the query safety gate (Aadhaar/phone/bank list requests → logged refusal); stopword-hardened coverage signal; audit log rotated after multi-process dev corruption (single-writer constraint documented below) | gold G-030..034 pass |

### Baseline after session 4 (mock LLM, BM25-only, seed corpus)
| Metric | Value | Trend |
|---|---|---|
| M1 citation completeness | **100%** | = |
| M4 refusal correctness | **93.8%** | ↑ from 75 |
| M6 rbac+injection refusal | **100%** | new |
| retrieval must-cite hit | 92.3% | = |
| latency p50/p95 | 15/39 ms | = |
| answer rate | 54.2% | blocked on vector leg (Hindi + recall) |

Remaining failures (12): 6 Hindi cross-lingual + 4 EN recall (both fixed by the
vector leg with real corpus), G-026 superlative handling, G-040 emotional-support
routing — all tracked, none regressions.

### Constraint discovered
Phase6e audit log assumes a SINGLE writer process. Concurrent dev processes forked
the hash chain (detected by verify_chain — the tamper-evidence working as designed).
Rule: one app process per data dir; delivery design uses the Kafka audit stream (RFP)
which removes the constraint. Old dev log archived as events.dev-smoke-archive.jsonl.

### Demo runbook (2 minutes, fully offline)
1. `SUBSTRATE_RAG=true LLM_PROVIDER=mock uvicorn backend.main:app --port 8000`
2. Open **/substrate-demo** → sign in as `meena` → ask the healthcare question →
   show citation chips + gates → ask about a robotics course → honest refusal.
3. Sign in as `rajesh` → Analytics: "which centres have low attendance?" →
   TC-DEL-002 deteriorating, SQL disclosure → try "ALL districts" → still scoped.
4. Drafts: generate → review → approve → show immutability.
5. Audit tab: chain verified ✓ badge → every action from steps 2-4 visible.
6. Registry tab: manifest + corpus hash (deterministic versioning, RFP 4.B.1c).

**Test state: 33/33 · gold set: 40/40 items executing · M1 100 / M4 93.8 / M6 100**

---
## Session 5 — Content QA, injection quarantine & feedback loop (2026-07-19, later)

| Story | What landed | Verify |
|---|---|---|
| ST-205 | Injection quarantine AT INGESTION: chunks carrying prompt-injection payloads are forced to sensitivity=restricted / admin-only and flagged — they can never enter a learner/officer/sme prompt. Red-team plant doc (`rt-injection-plant`) lives in the corpus permanently: 2 of its 3 chunks auto-quarantined, benign chunk serves normally. Leak test proves no role/purpose combination retrieves quarantined chunks | `tests/test_substrate_contentqa.py` |
| ST-803 | `substrate/contentqa.py` + `/contentqa/{coverage,items}` — deterministic NOS-level coverage check (per-NOS covered/GAP with evidence chunk ids; flags courses not declared for the QP) and Bloom-tagged assessment item drafts (LLM when live, template fallback offline), all review_status=pending. SME/admin only; Content QA tab added to the demo console | UI: sign in as `iyer` |
| ST-804 | `/feedback` — 👍/👎 on every answer in the console; 👎 auto-queues the interaction as a candidate gold item in `evals/gold_candidates.jsonl` (needs SME rubric before promotion). The failure→eval flywheel from the PRD is live | thumbs in chat meta row |

**Test state: 42/42 passing · gold set 40/40 executing · M1 100 / M4 93.8 / M6 100**
Corpus: 10 docs → 40 chunks (2 quarantined) · manifest `man-b30ca1faa4c9`

All three PoC personas now have working surfaces: learner (grounded mentor),
officer (scoped analytics + maker-checker drafts), SME (coverage checks + item
drafts). Buildable-without-unlocks backlog is now essentially clear — remaining
work needs the Sarvam key, real QP PDFs, and the docker stack (vector + KG legs),
plus proposal artifacts (ST-1301..1305) which can be drafted anytime.

---
## Session 6 — Proposal artifact pack (2026-07-19, later)

`docs/proposal/` now contains the five bid-collateral documents (ST-1301–1305),
all grounded in measured PoC evidence rather than promises:
1. **01_architecture_blueprint.md** — the Q1 contractual deliverable draft; full §4.B layer mapping with "PoC: implemented" evidence per requirement.
2. **02_model_and_system_card.md** — per §4.B.3; includes measured eval table and honest known-limitations list.
3. **03_saicr_process.md** — change-gate with hard blocks (M1<100%, RBAC<100% ⇒ no ship); PoC registry/harness already produce every required artefact.
4. **04_dpdp_consent_lineage_design.md** — consent lifecycle + source-to-answer lineage chain as implemented; delivery gap list.
5. **05_evaluation_report.md** — methodology, baseline numbers, full failure taxonomy, adversarial results, next measurement milestones.

## ROADMAP — next phases & actions

### Phase A — Unlocks (human, ~1 day effort, highest leverage)
| # | Action | Owner | Unblocks |
|---|---|---|---|
| A1 | Rotate Sarvam API key; confirm chat + embeddings quota (ST-105) | Pankaj | Live composition, groundedness judge (first hallucination number), item-draft LLM mode |
| A2 | Download 11 registered public docs → corpus/raw, set status=DOWNLOADED, re-ingest | DATA owner | Real-corpus eval run; removes all SEED caveats |
| A3 | `docker compose -f docker-compose.substrate.yml up -d` on a dev box; `ingest --qdrant`; run kg/loader.py | any dev | Vector leg (fixes 10 Hindi/recall gold failures), live KG pathway leg, Jaeger traces |
| A4 | IP/legal sign-off for phase6e reuse in bid (ST-1306) | Management | Sharing the repo/demo beyond the team |

### Phase B — Team pilot (Week of unlocks +1..2)
- B1: Re-run harness on live stack → update eval report + model card numbers.
- B2: Onboard 5–8 internal testers on /substrate-demo with the 15-scenario script (PRD §13); log every failure via 👎 → gold_candidates.
- B3: Expand gold set to 80 items with SME rubrics (promote candidates).
- B4: Judge calibration vs SME on 30 samples.
- B5: Latency tuning with live LLM (streaming, caching) toward P95 targets.

### Phase C — Demo & bid readiness (Weeks 3–4)
- C1: Rehearse the 20-min PRD §14 demo script ×2 (incl. offline fallback).
- C2: Record demo video; polish proposal pack (convert to docx/pptx as needed).
- C3: Compliance matrix: story→FR→RFP §4.B→KPI traceability table into the bid.
- C4: Decide stretch demos: voice vignette (Saaras/Bulbul), WhatsApp teaser, model-duality toggle (ST-1201–1204).

### Phase D — Post-bid / delivery prep (as needed)
- KG coverage expansion (12+ QPs), Keycloak swap, Kafka audit stream,
  ClickHouse events, claim-level policies (superlatives), intent routing of
  emotional-support queries, PASE/predictive surface designs per Q3–Q6 plan.

**Current confidence:** demoable offline today; all deltas to a bid-winning
demo sit behind Phase A actions.

---
## Session 7 — Bid-readiness pack & unlock attempts (2026-07-19, later)

| Item | Outcome |
|---|---|
| ST-105 Sarvam key validation | **Attempted from sandbox — network blocked (proxy 403 to api.sarvam.ai).** Key IS present in .env (36 chars). Run the one-line curl in DEMO_SCRIPT.md pre-flight on any internet machine to validate, then `LLM_PROVIDER=sarvam`. Rotation still recommended before sharing. |
| A2 public docs fetch | **Attempted — PDFs not retrievable from this environment.** Manual download remains (11 rows in SOURCE_REGISTER, ~1 hr of browser work). |
| C3 compliance matrix | `docs/proposal/06_compliance_traceability_matrix.md` + `.csv` — all 27 Part B requirement rows mapped RFP→FR→story→KPI→status→evidence; ✅/🟦/🗓 status per row; measured KPI evidence footer. Drop-in for the bid workbook. |
| C1 demo script | `DEMO_SCRIPT.md` — 6-scene, 20-min presenter script with exact queries (EN+HI), what to point at per scene, pre-flight checklist, offline fallback plan, and Q&A ammunition (incl. the honest hallucination answer). |

## ROADMAP (updated)

### Phase A — Unlocks (human; unchanged, now with helpers)
- A1 Sarvam key: validate via the curl in DEMO_SCRIPT pre-flight → set `LLM_PROVIDER=sarvam`. (Sandbox cannot reach the API — must be your machine.)
- A2 Real docs: manual download per SOURCE_REGISTER → `python -m backend.substrate.ingest`.
- A3 Docker stack + `ingest --qdrant` + `python backend/substrate/kg/loader.py`.
- A4 IP/legal sign-off (ST-1306) — only remaining blocker for external sharing.

### Phase B — Team pilot (after A): live harness re-run → update docs 02/05 numbers · 5–8 testers on the demo script scenes · gold set → 80 items · judge calibration · latency tuning.

### Phase C — Bid submission: rehearse ×2 · record video · convert proposal pack to docx/pptx for the bid volume (ask me — I can generate these) · stretch demos (voice/WhatsApp/model-duality) if A1+A3 done.

### Phase D — Delivery prep: Keycloak, Kafka audit, ClickHouse, KG expansion, claim-level policies, Q3–Q6 surface designs.

**Everything buildable inside this workspace is now built.** 42/42 tests · 40/40 gold items · demo console + script + 6-document proposal pack ready.

---
## Session 8 — Bid-volume deliverables (2026-07-19, later)

| Item | Outcome |
|---|---|
| C2 Word volume | `docs/proposal/NSDC_PartB_Technical_Volume_Draft.docx` — cover, TOC, 7 sections: exec summary, measured KPI table, §4.B architecture mapping, security/sovereignty, SAICR, compliance summary, delivery roadmap. Visually QA'd (6 pages). TOC updates on first open in Word. |
| C2 Pitch deck | `docs/proposal/NSDC_PartB_PoC_Deck.pptx` — 10 slides: dark title, thesis, architecture (block diagram embedded), four-gates pipeline (diagram embedded), measured stat cards, three-personas grid, governance chips, compliance snapshot, Q1–Q6 roadmap, dark close. pptx validation PASSED; every slide visually QA'd. |

## ROADMAP (unchanged priorities)
- **Phase A (yours):** Sarvam key validation (curl in DEMO_SCRIPT) · official docs download + re-ingest · docker stack + `ingest --qdrant` + KG loader · IP sign-off (ST-1306).
- **Phase B:** live harness re-run → refresh numbers in docx/deck/eval report · internal testers · gold set → 80 · judge calibration.
- **Phase C:** demo rehearsals ×2 · record video · final bid assembly (all collateral now exists in docs/proposal/).
- **Phase D:** delivery-prep engineering (Keycloak, Kafka, ClickHouse, KG expansion).

**All pre-bid collateral now exists:** 6 markdown artifacts + compliance CSV + Word technical volume + PPTX deck + demo script + working demoable PoC (42/42 tests, 40/40 gold items).

---
## Session 9 — Judge, superlative policy & preflight (2026-07-19, later)

| Item | What landed | Verify |
|---|---|---|
| ST-504 | `substrate/judge.py` — async groundedness judge: per-claim entailment verdicts from a SEPARATE judge provider (LLM_JUDGE_PROVIDER), fire-and-forget (never touches P95), scores persisted per interaction, honest mock behaviour (records "skipped" — hallucination rate is never fabricated). `/judge/stats` endpoint = the KPI 7.2.2 measurement surface. Activates automatically the moment a live model is enabled | 8 new tests |
| G-026 fix | Superlative gate: comparative/superlative asks ("best centre") refused unless the evidence bundle contains comparative numeric data from ≥2 sources (EN+HI patterns) | gold G-026 passes |
| Preflight | `preflight_substrate.py` — one command reporting exactly what's enabled/missing with the fix-it action per line; its warnings map 1:1 to Phase A items | run it on the demo box |

### Scoreboard after session 9
**Tests: 50/50 · M1 100% · M4 100% · M6 100%** · 11 residual gold failures, all
attributed: 10 = vector-leg/Hindi/recall (Phase A3), 1 = emotional-support
routing (design decision, Phase D). Judge wiring verified: 13 interactions
auto-recorded as "skipped" in mock mode.

### Engineering complete in this workspace
Every gate, surface, store, eval, artifact and helper buildable without live
services now exists. When Phase A completes, the live measurement pass is:
`python preflight_substrate.py` → `LLM_PROVIDER=sarvam python -m evals.run_harness`
→ `/judge/stats` gives the first real hallucination number → refresh docs 02/05
+ docx/deck numbers (ask me — one pass).

---
## Session 10 — Documentation refresh & cleanup (2026-07-19, later)
- NEW `README.md` — project front door: quickstart (run_substrate.bat), demo logins, what-works-today, repo map, key docs index, 5 operating rules.
- NEW `design.md` — NSDC substrate design doc: request-path walkthrough, data-layer decisions, 3-point access-control model, eval-first design, reuse boundary, scale path, known limitations. (Old phase6e gov-services design.md removed.)
- REMOVED obsolete phase6e docs: CONCERNS, DEMO_GUIDE, DEMO_RUNBOOK (superseded by DEMO_SCRIPT), PHASE6E_README, PHASE6E_REVIEW_REPORT, PHASE6F_NOTES, PRODUCTION_READINESS, HARDENING_CHANGES, DEPLOYMENT, feature_list, validation, phase7 plan.
- MOVED still-useful provider/voice references → `docs/setup/` (LLM_PROVIDERS, SARVAM_DIAGNOSTICS, LIVEKIT×2, STREAMING_VOICE, TWILIO, VOICE_LATENCY, INSTALL_TROUBLESHOOTING).
- Task-plan workbook versions consolidated: master is `../NSDC_Task_Plan_v1.xlsx` (Sheet1 workstreams · Sheet2 activation+gaps · Sheet3 MASTER execution plan · RFP Alignment matrix); older copies deleted.
