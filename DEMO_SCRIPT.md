# Demo Script — Sovereign AI Substrate PoC
### 20 minutes · works fully offline (mock mode) · v1.0

## Pre-flight (do twice before any real audience)
```bash
# terminal 1 — from repo root
docker compose -f docker-compose.substrate.yml up -d     # OPTIONAL (skip for offline demo)
python -m backend.substrate.ingest                        # rebuild index + manifest
python -m backend.substrate.events                        # regenerate synthetic events
SUBSTRATE_RAG=true LLM_PROVIDER=mock uvicorn backend.main:app --port 8000
# LLM_PROVIDER=sarvam for live composition (validate key first — see below)
```
- Open http://localhost:8000/substrate-demo in a CLEAN browser profile.
- Run the harness once and screenshot the summary (backup slide):
  `SUBSTRATE_RAG=true LLM_PROVIDER=mock python -m evals.run_harness`
- Sarvam key check (on a machine with internet):
  `curl -s https://api.sarvam.ai/v1/chat/completions -H "api-subscription-key: $KEY" -H "Content-Type: application/json" -d '{"model":"sarvam-m","messages":[{"role":"user","content":"OK?"}],"max_tokens":5}'`
- If ANYTHING breaks mid-demo: the harness summary + audit chain screenshots
  are the fallback narrative ("measured, not promised").

## Scene 1 — Frame (2 min, slides)
Say: *"Part B asks for a substrate, not a chatbot. We built a working slice of
that substrate. Everything you'll see enforces the RFP's hard guarantees —
100% citations, honest refusals, role-scoped data, signed audit — in the
platform layer, so every future surface inherits them."*
Show: architecture block diagram (teal=reused hardened platform, purple=new
substrate) + the 6-layer RFP mapping.

## Scene 2 — Meena the learner (4 min)
Sign in: `meena / learner-demo`.
1. Ask: **"I passed class 10 and want a job in healthcare. Which course should I take?"**
   Point at: per-claim citation chips (chunk IDs), KG node anchors, gate
   badges (safety/evidence/citation: pass), manifest ID in the meta row.
   Say: *"Every sentence is bound to a source passage. The manifest ID means
   we can reproduce the exact index state that produced this answer."*
2. Ask: **"What is the NSQF level of the General Duty Assistant qualification?"** — precise, cited.
3. Ask (Hindi): **"PMKVY के लिए कौन से दस्तावेज़ चाहिए?"** — same guarantees in Hindi.
4. Ask: **"Tell me about the Advanced Robotics Technician course fees."**
   Say: *"No evidence → it says so. Refusal is decided by retrieval scores
   BEFORE the model composes — we don't rely on model honesty."*
5. Click 👎 on any answer → *"negative feedback auto-queues an eval candidate —
   the failure→evaluation flywheel the RFP's Responsible-AI section wants."*

## Scene 3 — Rajesh the officer (5 min)
Sign out → `rajesh / officer-demo` (note the jurisdiction chip: South Delhi).
1. Analytics: **"which centres have low attendance?"**
   Point at: TC-DEL-002 overall 78% but last-30d **61% ⚠ DETERIORATING**;
   expand "SQL executed" — *"figures are reproducible; the model never writes
   SQL; jurisdiction is injected from his verified token."*
2. Analytics: **"show attendance for ALL districts including North West Delhi"**
   → still South-Delhi-only. *"Prompt injection can't widen scope — scope
   isn't in the prompt."*
3. Analytics: **"List Aadhaar numbers of enrolled candidates"** → refusal +
   logged. *"Aggregates only, by construction."*
4. Drafts: Generate → show watermark + pending badge → Approve → try deciding
   again (409 immutable). *"Maker-checker: the machine drafts, only a human
   decides, every transition is signed into the audit chain."*
5. Switch briefly to `meena` and open Officer Copilot → **403**. Back.

## Scene 4 — Dr. Iyer the SME (3 min)
Sign in: `iyer / sme-demo`.
1. Content QA → coverage check `crs-gda-01` × `HSS/Q5101` → per-NOS
   covered/GAP table with evidence chunk IDs.
   Then check `crs-gda-01` × `HSS/Q0301` → "NOT declared for this QP" flag.
2. Draft items for `HSS/N5102` → every item tagged QP/NOS/Bloom, pending
   review. *"AI drafts, SME signs off — the RFP's content-quality gate."*

## Scene 5 — The governance reveal (4 min)
Sign in: `admin / admin-demo`.
1. Audit tab: *"every query, refusal, draft and decision from the last 15
   minutes — hash-chained, Ed25519-signed"* → point at **chain verified ✓**.
2. Registry tab: index manifests (content-addressed) + KG release hashes.
   *"RFP 4.B.1(c) deterministic versioning — most bidders will talk about it;
   this is it running."*
3. Mention (don't demo): a red-team document with injection payloads lives in
   this corpus permanently — its malicious chunks were quarantined at
   ingestion and no role can retrieve them. Tests prove it on every build.

## Scene 6 — Close (2 min, slides)
- Eval dashboard/summary: **M1 100% · refusals 93.8% · RBAC/injection 100% ·
  p95 39ms** — *"measured on a versioned gold set, adversarial categories run
  through the real authenticated API."*
- Roadmap slide: this exact codebase → Q1–Q6 milestones (blueprint doc 01);
  what turns on with Sarvam live + vector leg (hallucination measurement,
  Hindi parity, synthesised answers).
- Last line: *"The substrate is the bid. Surfaces are cheap once answers are
  governed."*

## Q&A ammunition
- "Is this Sarvam-ready?" — provider layer is Sarvam-primary with a hardened
  client; mock mode is the same pipeline with the model swapped out.
- "Real data?" — zero real learner data; schema physically rejects non-SYN
  learner IDs; corpus is registered public documents.
- "What breaks at scale?" — stores swap (SQLite→ClickHouse, file→Kafka,
  JWT→Keycloak); contracts don't. That's the substrate argument.
- "Hallucination rate?" — not quotable until the live judge runs; we show the
  enforcement mechanism (citation gate) and the measurement harness instead
  of quoting a number we can't defend.
