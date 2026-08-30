"""Compatibility shim — Phase 6c moved real RAG into `backend.retrieval`.

This module preserves the Phase 2-5 public API so the orchestrator,
admin sandbox, and any other caller continues to import `from backend.rag
import retrieve, load_corpora, corpus_stats`.

The new structured retrieval (chunks with metadata, citations, uploads,
cross-corpus reads) lives in `backend.retrieval`. Prefer importing from
there in new code.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import settings
from .retrieval import (
    Chunk, build_indices, corpus_stats, retrieve as _retrieve_v2, store,
)
from .retrieval.pipeline import ingest_jsonl, ingest_text

log = logging.getLogger("rag.shim")


def load_corpora() -> None:
    """Phase 6c loader.

    Behaviour:
      1. Load any existing `data/corpora/{agent}/chunks.jsonl` files.
      2. If the structured store is completely empty, seed from
         `data/personas/seed_corpora.jsonl` (the Phase 6c TN scheme pack).
      3. If still empty for an agent, fall back to migrating from a
         legacy `data/corpus/{agent}.txt` if present.
      4. Build BM25 indices.
    """
    total = store.load_all()

    # Seed-pack import (only if the structured store is empty)
    if total == 0 and settings.auto_seed_corpora:
        seed = Path(settings.data_dir) / "personas" / "seed_corpora.jsonl"
        if not seed.exists():
            # Project-shipped fallback (works in sandbox/test contexts)
            seed = Path(__file__).resolve().parent.parent / "data" / "personas" / "seed_corpora.jsonl"
        if seed.exists():
            try:
                raw = seed.read_text(encoding="utf-8")
                ingested = ingest_jsonl("__seed__", raw, uploaded_by="seed-pack")
                total += len(ingested)
                log.info("Seeded structured RAG from %s: %d chunks", seed.name, len(ingested))
            except Exception as e:
                log.warning("Failed to seed corpus from %s: %s", seed, e)
    elif total == 0 and not settings.auto_seed_corpora:
        log.warning("Structured RAG store is empty and AUTO_SEED_CORPORA=false; no demo corpus was imported.")

    # Per-agent legacy migration (only if that agent still has nothing)
    legacy_dir = Path(settings.data_dir) / "corpus"
    if legacy_dir.exists():
        for path in sorted(legacy_dir.glob("*.txt")):
            agent_id = path.stem
            if store.for_agent(agent_id):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except Exception:
                continue
            ingested = ingest_text(
                agent_id, raw,
                default_source=f"legacy:{path.name}",
                uploaded_by="legacy-migration",
            )
            log.info("Migrated legacy corpus %s → %d chunks", path.name, len(ingested))
            total += len(ingested)

    build_indices()
    log.info("RAG loaded: %d total chunks across %d agents",
             total, len(store.all_agent_ids()))


def retrieve(agent_id: str, query: str, k: int = 3,
              extra_corpora: list[str] | None = None) -> list[Chunk]:
    """Phase 6c retrieve — same signature as Phase 2.

    `extra_corpora` is new; the orchestrator passes the agent's
    `cross_corpus_read` allow-list here. Old callers that don't pass it
    behave exactly like Phase 2.
    """
    return _retrieve_v2(agent_id, query, k=k, extra_corpora=extra_corpora)


__all__ = ["load_corpora", "retrieve", "corpus_stats", "Chunk"]
