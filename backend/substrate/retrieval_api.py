"""Standalone retrieval API (ST-Versioning-Retrieval).

A queryable retrieval service over the versioned chunk corpus, independent
of the answer-composing /query pipeline — for callers that want ranked
evidence, not composed answers (eval harnesses, the officer console's
citation browser, external integrators).

Three legs, deliberately degradable:
  - bm25   : pure-Python lexical scoring over corpus/curated/chunks.jsonl
             (always available — no services needed)
  - vector : Qdrant similarity via backend.substrate.vector_store
             (available when an index has been built; local file mode works)
  - hybrid : Reciprocal Rank Fusion of both legs (default). If the vector
             leg is unavailable, hybrid degrades to bm25 with a logged note
             and `legs_used` in the response says exactly what ran — the
             API never silently pretends a leg contributed.

Every result carries: RBAC-safe chunk payload fields, per-leg and fused
scores, full source attribution (source_org/url/license/last_updated,
doc/section/page/timestamps), and the immutable index_manifest_id the
chunk was built under — so any result can be traced to the exact index
state that produced it.

RBAC (FR-6) is enforced in BOTH legs before ranking: role, purpose, and
sensitivity clearance. The vector leg enforces it in-store (Qdrant payload
filter); the bm25 leg enforces the identical predicate in-process.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from .schemas import ChunkPayload, Purpose, Role, SENSITIVITY_CLEARANCE

log = logging.getLogger("substrate.retrieval_api")

CURATED_CHUNKS = Path("corpus/curated/chunks.jsonl")
RRF_K = 60          # standard reciprocal-rank-fusion constant


def _tokenize(text: str) -> list[str]:
    # \w is Unicode-aware in Python — this must tokenize Devanagari (Hindi)
    # as well as Latin; a [a-z0-9]-only pattern silently makes every Hindi
    # chunk unsearchable in the bm25 leg of a bilingual corpus.
    return re.findall(r"\w+", text.lower())


# ---------------------------------------------------------------------------
# RBAC + filter predicate — one function used by BOTH legs so they can never
# drift apart (the vector leg's in-store filter mirrors this; the bm25 leg
# calls it directly).
# ---------------------------------------------------------------------------

def _visible(chunk: ChunkPayload, role: Role, purpose: Purpose) -> bool:
    if role not in chunk.allowed_roles:
        return False
    if purpose not in chunk.allowed_purposes:
        return False
    clearance = SENSITIVITY_CLEARANCE.get(role, ())
    return chunk.sensitivity in clearance


def _passes_filters(chunk: ChunkPayload, language: Optional[str],
                    doc_ids: Optional[list[str]],
                    section_contains: Optional[str],
                    source_mode: Optional[str]) -> bool:
    if language and chunk.language != language:
        return False
    if doc_ids and chunk.doc_id not in doc_ids:
        return False
    if section_contains and section_contains.lower() not in chunk.section.lower():
        return False
    if source_mode and chunk.source_mode != source_mode:
        return False
    return True


# ---------------------------------------------------------------------------
# BM25 leg — self-contained over ChunkPayload (the phase6e BM25Index is
# coupled to its own Chunk type and per-agent store; reusing it here would
# drag that store's lifecycle into this API for no ranking benefit).
# ---------------------------------------------------------------------------

class _Bm25Corpus:
    K1, B = 1.5, 0.75

    def __init__(self, chunks: list[ChunkPayload]):
        self.chunks = chunks
        self.tokens = [_tokenize(c.text + " " + c.section) for c in chunks]
        self.doc_len = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.N = len(chunks)
        self.inv: dict[str, list[tuple[int, int]]] = {}
        for i, toks in enumerate(self.tokens):
            for term, tf in Counter(toks).items():
                self.inv.setdefault(term, []).append((i, tf))
        self.idf = {t: math.log((self.N - len(p) + 0.5) / (len(p) + 0.5) + 1.0)
                    for t, p in self.inv.items()}

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for term in _tokenize(query):
            for idx, tf in self.inv.get(term, ()):
                denom = tf + self.K1 * (1 - self.B + self.B * self.doc_len[idx] / (self.avgdl or 1))
                scores[idx] = scores.get(idx, 0.0) + self.idf[term] * tf * (self.K1 + 1) / denom
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]


_CORPUS_CACHE: dict[str, _Bm25Corpus] = {}


def _load_corpus(path: Path = CURATED_CHUNKS) -> _Bm25Corpus:
    key = str(path.resolve())
    cached = _CORPUS_CACHE.get(key)
    mtime = path.stat().st_mtime if path.exists() else -1
    if cached is not None and getattr(cached, "_mtime", None) == mtime:
        return cached
    chunks: list[ChunkPayload] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    chunks.append(ChunkPayload.model_validate_json(line))
    corpus = _Bm25Corpus(chunks)
    corpus._mtime = mtime          # type: ignore[attr-defined]
    _CORPUS_CACHE[key] = corpus
    return corpus


# ---------------------------------------------------------------------------
# Result shaping — attribution on EVERY result, no exceptions
# ---------------------------------------------------------------------------

def _to_result(chunk: ChunkPayload, fused: float,
               bm25_score: Optional[float], vector_score: Optional[float]) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "language": chunk.language,
        "score": round(fused, 6),
        "scores": {"bm25": bm25_score, "vector": vector_score},
        "attribution": {
            "source_org": chunk.source_org,
            "source_url": chunk.source_url,
            "source_license": chunk.source_license,
            "source_last_updated": chunk.source_last_updated,
            "doc_id": chunk.doc_id,
            "section": chunk.section,
            "page": chunk.page,
            "start_ts": chunk.start_ts,
            "end_ts": chunk.end_ts,
            "source_mode": chunk.source_mode,
            "ocr_confidence": chunk.ocr_confidence,
        },
        "quality_flags": chunk.quality_flags,
        "index_manifest_id": chunk.index_manifest_id,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def retrieve(query: str, role: Role, purpose: Purpose,
             top_k: int = 8, mode: str = "hybrid",
             language: Optional[str] = None,
             doc_ids: Optional[list[str]] = None,
             section_contains: Optional[str] = None,
             source_mode: Optional[str] = None,
             corpus_path: Path = CURATED_CHUNKS,
             vector_store=None) -> dict:
    """Run retrieval and return {results, legs_used, index_manifest_id,
    total_candidates}. `vector_store` is injected (a VectorStore already
    holding an embed fn) — the route layer decides whether one is available
    rather than this function guessing at global state.
    """
    if mode not in ("bm25", "vector", "hybrid"):
        raise ValueError(f"unknown retrieval mode: {mode}")
    top_k = max(1, min(top_k, 50))

    corpus = _load_corpus(corpus_path)
    visible: list[tuple[int, ChunkPayload]] = [
        (i, c) for i, c in enumerate(corpus.chunks)
        if _visible(c, role, purpose)
        and _passes_filters(c, language, doc_ids, section_contains, source_mode)]
    visible_idx = {i for i, _ in visible}

    legs_used: list[str] = []
    bm25_rank: dict[str, tuple[int, float]] = {}     # chunk_id -> (rank, score)
    vec_rank: dict[str, tuple[int, float]] = {}
    by_id: dict[str, ChunkPayload] = {c.chunk_id: c for _, c in visible}

    if mode in ("bm25", "hybrid") and corpus.chunks:
        hits = [(i, s) for i, s in corpus.search(query, top_k * 4) if i in visible_idx]
        for rank, (i, s) in enumerate(hits[:top_k * 2], 1):
            bm25_rank[corpus.chunks[i].chunk_id] = (rank, s)
        legs_used.append("bm25")

    if mode in ("vector", "hybrid"):
        if vector_store is not None:
            try:
                vhits = vector_store.search(query, role, purpose,
                                            top_k=top_k * 2, language=language)
                filtered = [(c, s) for c, s in vhits
                            if _passes_filters(c, language, doc_ids,
                                               section_contains, source_mode)]
                for rank, (c, s) in enumerate(filtered, 1):
                    vec_rank[c.chunk_id] = (rank, float(s))
                    by_id.setdefault(c.chunk_id, c)
                legs_used.append("vector")
            except Exception as e:
                log.warning("vector leg failed (%s) — continuing without it", e)
        elif mode == "vector":
            raise RuntimeError("vector retrieval requested but no vector index "
                               "is available — build one with "
                               "`python -m backend.substrate.ingest --qdrant`")
        else:
            log.info("hybrid requested but vector leg unavailable — bm25 only")

    # Reciprocal Rank Fusion across whichever legs actually ran
    fused: dict[str, float] = {}
    for ranks in (bm25_rank, vec_rank):
        for cid, (rank, _s) in ranks.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    results = [
        _to_result(by_id[cid], score,
                   bm25_score=bm25_rank.get(cid, (None, None))[1],
                   vector_score=vec_rank.get(cid, (None, None))[1])
        for cid, score in ordered if cid in by_id]

    manifest_ids = {r["index_manifest_id"] for r in results if r["index_manifest_id"]}
    return {
        "query": query,
        "mode": mode,
        "legs_used": legs_used,
        "results": results,
        "total_candidates": len(visible),
        "index_manifest_id": manifest_ids.pop() if len(manifest_ids) == 1 else
                             (sorted(manifest_ids) if manifest_ids else None),
    }
