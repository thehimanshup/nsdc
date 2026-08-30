# DPDP Consent & Data-Lineage Design Note (v0.9)
### Per RFP §4.B.1(e) and §4.B.7(g) — DPDP Act 2023 alignment for the AI substrate

## 1. Principles
1. **Purpose-bound by construction.** Every AI interaction carries a consent
   token `{user, purpose, scope, issued_at, expires_at, revocable}`; purpose
   is checked at retrieval (chunks carry `allowed_purposes`) — content
   ingested for one purpose cannot be served for another.
2. **Fail closed.** Missing consent → reject. Missing metadata → document
   never ingested. Role not listed → chunk never retrieved. Uncited claim →
   answer withheld.
3. **Data minimisation.** Learner-facing surfaces operate on public corpus
   content only; analytics serve aggregates only (PII-harvest requests are
   pattern-refused and logged); personal identifiers are redacted from logs,
   model context and storage by the platform redaction layer.
4. **Evidence, not assertion.** Every control above is testable and tested —
   the PoC's adversarial suite exercises each one on every run.

## 2. Consent lifecycle (implemented pattern)
Grant (UI, per purpose: course_guidance / scheme_admin / content_qa) →
token issued and recorded in the **Ed25519-signed, hash-chained consent
ledger** → token attached to every downstream call and stored with the audit
record → **revocation** endpoint halts further processing; `verify_chain()`
proves ledger integrity. Time-bound expiry (PoC: 24h) forces re-consent.
Delivery: alignment with the DPDP Consent-Manager framework and account-
aggregator-style artefacts; consent receipts to users.

## 3. Source-to-answer lineage (implemented pattern)
Every answer is reconstructable end-to-end:

    SOURCE_REGISTER row (org, url, version, license, sensitivity, roles, purposes)
      → document → chunks (chunk_hash, section, page)
      → index manifest (embedding model + chunking config + corpus snapshot + KG release — content-addressed)
      → retrieval set (chunk IDs + KG node IDs, RBAC-filtered)
      → citation contract (per-claim citation IDs)
      → audit record (actor, role, purpose, consent token, gates, latency, manifest ID)

Embedding-level lineage per §4.B.1(e): the manifest binds every vector to the
exact corpus snapshot and model that produced it; re-index ⇒ new manifest ⇒
old answers remain attributable to their original index state.

## 4. Transactional data (§4.B.1f)
Canonical events carry `consent_token_id` per event. PoC uses synthetic data
with a schema-level guard (learner IDs MUST be SYN- prefixed — real
identifiers are rejected by validation). Delivery: consent enforcement at
event ingestion; purpose-scoped feature-store reads; retention schedules per
record class.

## 5. DSR & breach readiness (§4.B.7g)
DSR endpoints (access/erasure workflow) exist in the base platform and are
re-verified in the PoC. Delivery adds: DPO designation, RoPA maintenance,
DPIA per new feature (SAICR captures the trigger), breach-notification
runbook with regulator timelines, and cross-border controls (India-resident
by default; any cross-border processing only with explicit NSDC/GoI
authorisation).

## 6. Residency
Model weights, embeddings, KG data, corpora, audit and consent ledgers,
inference endpoints: India-resident (ap-south-1 / MeitY-empanelled). Mock/
offline mode demonstrates zero-egress operation.

## 7. Gaps to close at delivery (tracked)
Consent-manager UX & receipts · retention/erasure automation across stores
(including vector deletion propagation — manifest rebuild policy) · DPIA
templates wired into SAICR · Kafka audit stream replacing single-writer file
log · formal RoPA.
