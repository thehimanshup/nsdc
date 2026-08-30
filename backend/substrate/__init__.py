"""Sovereign AI Substrate extensions — NSDC SIDH Part B PoC.

New modules layered on the phase6e foundation:

    schemas.py    — governed data contracts (document metadata, chunks,
                    citation contract, canonical events, consent, evals)
    manifest.py   — deterministic index versioning (RFP 4.B.1c)
    kg/           — knowledge graph bootstrap + loaders (RFP 4.B.1a/1g)
    vector_store. — Qdrant adapter with RBAC payload filters (RFP 4.B.1c)
    retriever.py  — hybrid fusion: vector + BM25 + KG (RFP 4.B.1d)
    gates.py      — evidence gate + citation hard gate (KPI 7.2.2/7.2.4)

Everything here is additive: phase6e modules (consent, audit, llm, safety)
are imported, not modified, wherever possible.
"""
