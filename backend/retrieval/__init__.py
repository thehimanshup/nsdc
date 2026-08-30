"""Phase 6c — structured per-agent RAG.

The Phase 2/5b `backend/rag.py` worked but had limits: flat .txt corpora,
no metadata, no citations, no way for citizens or admins to upload their
own documents. Phase 6c replaces that with:

  - **Structured chunks** with rich metadata (scheme_id, eligibility,
    helpline, source, last_verified, …) stored as JSONL per agent.
  - **Hybrid scoring** — BM25 keyword + metadata-aware boost (e.g. an
    exact scheme-name match wins over a fuzzy semantic match).
  - **Citation support** — every chunk has a stable id and source which
    the orchestrator can render as a citation chip.
  - **Hot upload** — admin uploads .txt/.md/.json/.jsonl files, the
    ingest pipeline auto-chunks + persists, and the BM25 index is
    rebuilt on the fly.
  - **Cross-corpus reads** — Health agent can pull from CMO welfare
    if its persona's `cross_corpus_read` list includes "cmo".

Public surface — `from backend.retrieval import store, retrieve, …`
"""
from __future__ import annotations

from .chunk_store import Chunk, ChunkStore, store
from .bm25 import BM25Index
from .pipeline import (
    retrieve, retrieve_with_meta, build_indices, refresh_agent,
    ingest_text, ingest_jsonl, corpus_stats,
    delete_chunk, list_chunks_for_agent, get_chunk,
)

__all__ = [
    "Chunk", "ChunkStore", "store",
    "BM25Index",
    "retrieve", "retrieve_with_meta", "build_indices", "refresh_agent",
    "ingest_text", "ingest_jsonl", "corpus_stats",
    "delete_chunk", "list_chunks_for_agent", "get_chunk",
]
