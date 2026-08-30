# SAICR — Sovereign AI Change Review (Process Draft v0.9)
### Per RFP §4.B.11(d): "SAICR process gates every model/adapter change prior to deployment"

## 1. Scope — what triggers a SAICR
Any change that can alter what an AI surface says or who can see what:
generation model or version; adapter (LoRA) add/update; embedding model or
chunking config (⇒ new index manifest); KG ontology or release; prompt/
citation-contract change; gate thresholds (evidence, citation, safety);
RBAC vocabulary (roles, purposes, sensitivity clearances); corpus additions
beyond routine registered documents.

**Not in scope:** UI copy, infra scaling, routine registered-document
ingestion (covered by the register's own controls).

## 2. The gate (definition of done for any change)
A change ships only when its SAICR record contains:

1. **Change description** — what, why, RFP/story reference, owner.
2. **Version evidence** — old→new identifiers: model id, adapter id, index
   manifest id (content-addressed), KG release hash. *(The PoC registry
   makes these mechanical: every answer already carries its manifest ID.)*
3. **Eval evidence** — full gold-set harness run on the change branch:
   - KPI table vs current production baseline (M1 citation completeness,
     hallucination/groundedness, refusal correctness, RBAC/injection
     refusal, latency p50/p95).
   - **Hard blocks:** M1 < 100% · RBAC/injection refusal < 100% ·
     hallucination above agreed threshold · p95 regression > 20%.
   - Red-team suite pass (incl. permanent injection-plant checks).
4. **Bias & language evidence** — Hindi (and per-rollout languages) subset
   results; bias probe set for surface-relevant protected attributes.
5. **Rollback plan** — previous manifest/model IDs retained and re-activable;
   for adapters: previous adapter pinned; for KG: previous release tag.
6. **Approvals** — engineering owner + Responsible-AI reviewer; NSDC sign-off
   for production-user-facing changes (per governance agreement).
7. **Audit anchor** — SAICR record ID written to the audit stream; the
   record links the eval run artefacts (results JSONL + summary).

## 3. Cadence & roles
- **Standard changes:** async review, 2 approvers, ≤2 business days.
- **Emergency (security/safety):** ship-then-review within 24h, mandatory
  retrospective.
- **Quarterly re-tuning** (per §4.B.2.1 SLM cadence): batched SAICR with
  full multilingual eval + psychometric checks where assessment models are
  touched (parallel-forms reliability, measurement invariance per §4.B.3).
- Roles: Change owner (engineer) · RAI reviewer (independent of owner) ·
  NSDC product authority (user-facing) · Security reviewer (RBAC/gate changes).

## 4. Records & transparency
SAICR records are append-only and auditable (audit stream + evidence vault).
Public model/system cards (doc 02) are updated on every accepted SAICR that
changes user-visible behaviour. Recurring summary to NSDC monthly.

## 5. PoC → delivery
The PoC already produces every artefact the gate needs (manifests, KG hashes,
harness runs, red-team suite, audit anchors). Delivery adds: the formal
approval workflow (ticketed), the evidence vault, NSDC sign-off integration,
and threshold values contracted at KPI-finalisation (7.2.x "to be agreed"
items).
