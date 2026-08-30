# Compliance Traceability Matrix — Part B §4.B
### RFP requirement → PRD FR → story → KPI → PoC evidence · v0.9 (2026-07-19)

**Status legend:** ✅ Demonstrated in PoC (working code + tests) · 🟦 Designed
(documented pattern, delivery-phase build) · 🗓 Planned per milestone quarter.

| RFP ref | Requirement | PRD FR | Stories | KPI | Status | PoC evidence |
|---|---|---|---|---|---|---|
| 4.B.1(a) | Canonical Skilling Knowledge Graph | FR-3 | ST-301/302/303/305 | — | ✅ (v0.1, 3 QPs) | `substrate/kg/` ontology+loader; hash-versioned releases; pathway query; registry view |
| 4.B.1(b) | Indic skilling corpora, provenance, quality-rated | FR-1/2 | ST-201/202/203 | — | ✅ (governed pipeline; EN+HI content pending real docs) | `corpus/SOURCE_REGISTER.csv`; fail-closed `DocumentMeta`; ingest pipeline |
| 4.B.1(c) | Vector stores, deterministic versioning | FR-4/6 | ST-204/401 | — | ✅ manifests · 🟦 vector store (code ready, infra pending) | `substrate/manifest.py` (content-addressed); `vector_store.py` w/ in-store RBAC |
| 4.B.1(d) | Graph-RAG runtime, full traceability | FR-6/7 | ST-403/404/501 | 7.2.4 | ✅ | Hybrid retriever + citation contract; **measured 100% citation completeness** |
| 4.B.1(e) | DPDP governance, lineage, consent | FR-18 | ST-602 | 7.3.1 | ✅ (PoC-grade) | Ed25519 consent ledger; purpose-bound tokens; lineage chain (doc 04) |
| 4.B.1(f) | Transactional intelligence layer | FR-5 | ST-701 | — | ✅ (synthetic, SQLite) | Canonical event schema w/ SYN- guard; 9,668-event generator; scoped analytics |
| 4.B.1(g) | Super-taxonomies & crosswalks | FR-3 | ST-304 | — | ✅ (sample) | NSQF/Bloom nodes + ESCO crosswalk edges in KG |
| 4.B.2 | Sovereign LLM fine-tuning pipeline | — | (delivery) | — | 🗓 Q1→Q3 | Provider abstraction live (Sarvam primary); LoRA plan in blueprint §Layer-4 |
| 4.B.2.1(a-d) | SSC/DGT/NCVET SLMs, cross-sector embeddings | — | (delivery) | — | 🗓 Q3→Y2 | Edge-quantised path designed; embeddings slot in manifest |
| 4.B.3 | Safety & Responsible AI programme | FR-8/9/20/21/22/23 | ST-502/503/504/521/1001/1002/1005 | 7.2.2/7.2.3 | ✅ gates+harness · 🟦 judge (needs live model) | 3 sync gates; 40-item gold set; red-team suite; model card (doc 02) |
| 4.B.4.1 | AI Skill Mentor | FR-10/13 | ST-801/901 | 7.2.1/7.2.4 | ✅ (EN+HI, web; voice/WhatsApp = stretch assets present) | `/substrate-demo` mentor tab; bilingual gold results |
| 4.B.4.2 | Officer Copilots (RBAC, insights, maker-checker) | FR-14/17 | ST-601/702/703/1004 | — | ✅ | Token-scoped analytics; draft-note approve/reject; 12 adversarial tests |
| 4.B.4.5 | Content authoring w/ SME review | FR-15 | ST-803 | 7.4.3 | ✅ (items+coverage; authoring at delivery) | NOS coverage checks; Bloom-tagged pending items |
| 4.B.4.6 | Voice/IVR | — | ST-1201 (stretch) | — | 🟦 | Saaras/Bulbul/LiveKit/Twilio stack present in base platform |
| 4.B.4.8 | AI assessment models | — | (delivery) | — | 🗓 Q3+ | Item-draft pipeline + review pattern is the seed |
| 4.B.5(b/e) | Agent identity, Action Gateway maker-checker | — | (delivery) | — | 🟦 | Maker-checker pattern proven on drafts; signed-audit foundation |
| 4.B.5(d) | MCP / A2A protocol support | — | ST-1204 (stretch) | — | 🟦 | MCP client + India-egress governance in base platform |
| 4.B.6(a-f) | W3C VC / SD-JWT / HSM | — | (delivery) | — | 🗓 Q1-Q4 | Blueprint §Layer-6 |
| 4.B.7(g) | DPDP compliance programme | FR-18/19 | ST-602/604 | 7.3.1/7.3.2 | ✅ patterns · 🟦 programme (DPO/RoPA/DPIA) | Doc 04; DSR endpoints; PII redaction |
| 4.B.7(a-f,h,i) | CVD, TLS, CERT-In, ASVS/MASVS, WebAuthn | — | (delivery Q1) | 7.3.3/7.3.4 | 🗓 Q1 | Delivery hardening checklist in blueprint §3 |
| 4.B.8 | Content quality & curation gates | FR-15 | ST-803 | 7.4.1/7.4.2 | ✅ (checks) · 🗓 sweep at scale | Placeholder/metadata flags; coverage verdicts |
| 4.B.9 | Open APIs & developer ecosystem | — | (delivery Q4) | — | 🟦 | FastAPI OpenAPI 3.x auto-published today |
| 4.B.10 | State federation | — | (delivery Y3+) | 7.5.5 | 🗓 | Jurisdiction-scoped RBAC is the primitive |
| 4.B.11(a) | Data sovereignty | NFR-3 | — | — | ✅ posture | India-resident plan; zero-egress offline mode demonstrated |
| 4.B.11(b) | Observability & cost discipline | FR-24 | ST-1102 | 7.2.5 | ✅ partial | OTel hooks + latency/gates per interaction; cost meter at live-model step |
| 4.B.11(c) | Accessibility WCAG 2.1 AA | NFR-5 | (delivery) | — | 🗓 | Baseline in console; audit at delivery |
| 4.B.11(d) | Auditability & SAICR | FR-16/19 | ST-603/1101/1301-03 | — | ✅ | Hash-chain + Merkle roots + verify badge; SAICR draft (doc 03) |

**Measured KPI evidence (baseline v1):** 7.2.4 citation completeness **100%** ·
refusal correctness **93.8%** · RBAC/injection refusal **100%** · p50/p95
15/39 ms (pipeline) · 7.2.2 pending live judge. Full methodology: doc 05.

*CSV twin of this table: `06_compliance_traceability_matrix.csv` (for the bid
workbook).*
