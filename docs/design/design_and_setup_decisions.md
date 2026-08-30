# Design & Setup — decisions record

Covers: vector DB choice · chunking strategy (text/PDF/HTML/video-transcript)
· Sarvam embedding model selection · store schema (structured vs
unstructured) · deterministic versioning scheme.

This records *decisions*, not just options — where a decision required
information I can't verify from inside this environment (no live network
to Sarvam, no ability to confirm today's exact model catalog), that's
flagged explicitly rather than guessed at.

---

## 1. Vector DB: **Qdrant** (confirmed, not new — already what's implemented)

`backend/substrate/vector_store.py` already targets Qdrant. Keeping it,
for reasons that matter for this RFP specifically:

| Requirement | Why Qdrant fits |
|---|---|
| Data sovereignty / DPDP | Self-hostable, Rust, no managed-SaaS dependency — the whole "sovereign substrate" framing breaks if the vector store is someone else's cloud API |
| RBAC filtering *inside* retrieval (FR-6, the security invariant this PoC leans on hardest) | Native payload filtering (`FieldCondition`/`MatchAny`) lets role/purpose/sensitivity filtering happen in the same query as the ANN search — a chunk a role can't see is never returned, not filtered out after the fact |
| Multilingual EN/HI corpus | Payload filter on `language` field composes with the vector search in one call (already wired in `VectorStore.search`) |
| Ops footprint for a PoC → production path | Single binary/container, no separate index-build step, `docker-compose.substrate.yml` already has it |

**Alternative considered and rejected for now:** `pgvector` (if the
deployment already runs Postgres for other state, one fewer service to
operate). Rejected because the RBAC-in-store design here depends on rich
payload filtering that's more natural in Qdrant's filter DSL than in SQL
`WHERE` clauses bolted onto a vector column — revisit only if ops constraints
force consolidation onto Postgres.

**Action item, not a design gap:** the store choice isn't yet part of the
deterministic manifest hash (see §5) — fixed below.

---

## 2. Chunking strategy per source type

Current strategy (`backend/substrate/ingest.py`) is **section-aware with
word-based fallback splitting**: split on markdown/ALL-CAPS headings first,
then fixed-window split within a section (`target_tokens=400`,
`overlap_tokens=60`). This is a genuine 2-level strategy, not a naive
fixed-window chunker, and it already generalizes across three of the four
source types:

| Source | Extraction | Chunking | Status |
|---|---|---|---|
| **Text (.md/.txt)** | Read as-is | Section-aware + overlap | ✅ already implemented |
| **HTML** | Strip `<script>/<style>`, strip remaining tags | Same section-aware splitter (headings from stripped text) | ✅ already implemented — works but is naive tag-stripping; if source HTML has semantic structure (`<h1>`–`<h4>`, `<table>`) worth swapping to a proper HTML parser (BeautifulSoup) so headings/tables survive structurally instead of as plain text lines. Flagging as a follow-up, not blocking. |
| **PDF** | `pdfplumber`, page-tagged (`<<<PAGE n>>>` markers preserved through to chunk metadata as `page`) | Same section-aware splitter | ✅ already implemented |
| **Video transcript** | — | — | ❌ not implemented before this change |

**Video-transcript chunking — implemented now** (`_extract_transcript` +
`chunk_document` timestamp path in `ingest.py`):

- Parses **SRT and WebVTT** cue format (`HH:MM:SS,mmm --> HH:MM:SS,mmm` /
  `HH:MM:SS.mmm --> ...`), the two formats virtually every transcription
  pipeline (including Sarvam's own STT) emits.
- Chunking rule: **merge consecutive cues into ~400-token windows** (same
  `target_tokens`/`overlap_tokens` config as text, so it's one shared
  `ChunkingConfig` — not a second parallel config to keep in sync), but
  **never split a chunk boundary across the overlap** — a chunk's
  `start_ts`/`end_ts` always span whole cues, so a citation can link back to
  an exact, playable moment in the source video rather than an approximate
  offset.
- New optional `ChunkPayload` fields: `start_ts` / `end_ts` (seconds,
  float). Optional and default `None` so no existing chunk record needs
  migration.
- No heading-based section split for transcripts (there usually isn't one)
  — section field is set to a synthetic `"Segment {n}"` label instead, so
  the UI's citation display code doesn't need a special case for missing
  sections.

**Not implemented, explicitly out of scope for now:** table-aware chunking.
`ChunkingConfig.keep_tables_intact` already exists as a field but isn't
enforced anywhere in `chunk_document()` — it's a no-op flag today. Given the
QP/NOS source documents are table-heavy (eligibility criteria, assessment
criteria tables), this is a real gap, not a hypothetical one — recommend
picking it up before the next corpus ingestion pass, using the HTML-parser
follow-up above as the natural place to detect `<table>` and keep it as one
atomic chunk regardless of token count.

---

## 3. Sarvam embedding model — **decision deferred, correctly, not guessed at**

I want to be direct about a limit here rather than assert something I can't
verify: I don't have live network access to `api.sarvam.ai` from this
environment (it's outside the sandbox's allowed egress list, and the
project's own status doc — `SUBSTRATE_POC_STATUS.md` — independently
confirms the same 403 when it tried), and I don't have reliable enough
knowledge of Sarvam's current embeddings product name/dimensions to assert
one without risking telling you something wrong. The code already reflects
this honestly: `sarvam_embedder()`'s docstring says *"model name to be
confirmed during key validation"* and probes the response for the actual
dimension rather than hardcoding it.

**What I did instead of guessing:**

- Left `sarvam_embedder(api_key, model="sarvam-embed", ...)` as a
  **pluggable, not-yet-pinned** default — the model string is a parameter,
  not baked into `VectorStore`, so confirming the exact name later is a
  one-line config change, not a code change.
- The **manifest already forces this to be explicit and auditable**:
  `IndexManifest.embedding_model` is a required field baked into the
  content-addressed `manifest_id` (§5) — so whichever model string you
  confirm, every chunk/answer traces back to exactly that choice, and if it
  changes later, that's a new manifest, not a silent drift.
- **Interim default stays BGE-M3** (`bge_m3_embedder()`, open, multilingual,
  1024-dim, already implemented) so the PoC vector leg can run *today*
  without Sarvam access, and swapping to Sarvam later is changing one call
  site (`get_service()` construction), not a redesign.

**Concrete next step** (not something I can do from here): run the
one-line curl already in `DEMO_SCRIPT.md` against `api.sarvam.ai` from a
machine with real internet access, confirm the exact embeddings endpoint
name + dimension, then set `model=` explicitly and re-run ingestion — the
new manifest_id that produces *is* the record of that decision.

---

## 4. Store schema: structured vs. unstructured sets

Two clearly separated sets already exist in the codebase; this section
makes the boundary explicit rather than inventing a new one.

### Unstructured set — free text for retrieval (RAG)
- **Shape:** `ChunkPayload` (schemas.py) — chunk text + RBAC metadata
  (`allowed_roles`, `allowed_purposes`, `sensitivity`, `language`) +
  provenance (`doc_id`, `section`, `page` or `start_ts`/`end_ts`) +
  `kg_node_ids` (graph anchors) + `index_manifest_id` (version anchor, §5).
- **Where it lives:** Qdrant (`VectorStore`, dense vector + payload) **and**
  a parallel BM25 export (`data/corpora/skilling_core/chunks.jsonl`) — the
  same chunk records serve both retrieval legs, not two divergent stores.
- **Access pattern:** similarity/keyword search, always RBAC-filtered
  in-store before anything reaches an LLM prompt.

### Structured set — typed records for direct lookup / joins
- **Shape:** typed Pydantic models with fixed relational-style fields, not
  free text — `SkillingEvent` (enrolment/certification/placement events,
  FK-like fields `centre_id`/`scheme_id`/`course_id`/`qp_code`),
  `ConsentToken` (purpose-bound, expiring), `GoldEvalItem` (eval harness
  rows), plus the flat JSON registries (`data/schemes.json`,
  `data/agents.json`, `data/sla_policies.json`).
- **Where it lives today:** flat JSON/JSONL files — fine at PoC scale
  (hundreds–thousands of rows), and it's what `backend/records/`,
  `backend/schemes.py`, `backend/substrate/events.py` already assume.
- **Access pattern:** exact lookup / filter (by centre, district, scheme),
  not similarity search — this is precisely the traffic the **Neo4j KG leg**
  (`kg_node_ids` on chunks, `retriever.py`'s KG pathway) is meant to absorb
  as the PoC scales past flat-file size, per the architecture blueprint's
  three-leg retrieval design. No new store needed here — the KG is already
  the intended structured-query answer, it's just not running yet (same gap
  flagged in the earlier tool/MCP review).

**The boundary rule, stated plainly:** if a field needs to be matched
*semantically* (a learner's free-text question), it belongs in the
unstructured set. If it needs to be matched *exactly* (a district code, a
scheme ID, "is this citizen's consent still valid"), it belongs in the
structured set. Nothing in the current schemas violates this — this section
documents the existing boundary so future additions don't blur it.

---

## 5. Deterministic versioning scheme

Already substantially implemented in `backend/substrate/manifest.py` and
genuinely well-designed: `IndexManifest.finalise()` computes a
content-addressed `manifest_id` from a canonical hash of
`{embedding_model, embedding_dim, chunking_config_hash, corpus_snapshot_hash,
kg_version, kg_content_hash}` — identical inputs deterministically produce
the identical ID, which is exactly the "any answer traces back to the exact
index state that produced it" property RFP 4.B.1(c) asks for.

**One real gap, fixed in this change:** the **vector store backend/version**
wasn't part of that hash — two manifests built with identical embeddings
and chunking config, but against different Qdrant versions or a swapped
store entirely (Qdrant → pgvector, say), would previously produce the
*same* `manifest_id` despite being different index states. Added:

```python
vector_store_backend: str = "qdrant"   # e.g. "qdrant" | "pgvector"
store_schema_version: str = "1"        # bump on breaking payload-schema changes
```

...both now included in the hashed manifest body, so the versioning scheme
genuinely covers all three axes you asked for: **embedding model + chunk
config + store version** — not just the first two.

See `backend/substrate/manifest.py` for the updated `IndexManifest` and
`tests/test_substrate_core.py` / new coverage for the hash changing when any
of the three axes changes.
