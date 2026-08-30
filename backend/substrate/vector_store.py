"""Qdrant vector store adapter with RBAC payload filtering (ST-401/402).

Security invariant (PRD FR-6): role/sensitivity filtering happens HERE,
inside the store query, before any content can reach LLM context. The
caller passes the caller's Role + Purpose; chunks the role may not see
are never returned, so they can never be composed into a prompt.

Requires: pip install qdrant-client
Embedding model is injected (callable: list[str] -> list[list[float]]),
so Sarvam embeddings and BGE-M3 are interchangeable per ST-105 decision.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Callable, Optional, Sequence

from .schemas import ChunkPayload, Purpose, Role, SENSITIVITY_CLEARANCE

log = logging.getLogger("substrate.vector")

COLLECTION = "skilling_chunks"
EmbedFn = Callable[[Sequence[str]], list[list[float]]]


class VectorStore:
    def __init__(self, embed_fn: EmbedFn, dim: int,
                 url: Optional[str] = "http://localhost:6333",
                 path: Optional[str] = None,
                 collection: str = COLLECTION,
                 client=None):
        """`path` (Qdrant's local/serverless mode — a plain directory, no
        server process) takes priority over `url` when both are given.
        Local mode is what this PoC actually runs on in a sandbox with no
        Qdrant service reachable; `url` is the real deployment path once a
        Qdrant instance is up (docker-compose.substrate.yml already has one).

        Pass an existing `client` to share one Qdrant connection across
        multiple VectorStore instances/collections — local-mode Qdrant
        file-locks its storage directory, so two separately-constructed
        clients pointed at the same `path` will raise
        "already accessed by another instance"; reuse .client instead."""
        if client is not None:
            self.client = client
        else:
            from qdrant_client import QdrantClient  # lazy — optional dep
            self.client = QdrantClient(path=path) if path else QdrantClient(url=url)
        self.embed = embed_fn
        self.dim = dim
        self.collection = collection

    # -- lifecycle ----------------------------------------------------------
    def ensure_collection(self, recreate: bool = False) -> None:
        from qdrant_client.models import Distance, VectorParams
        if recreate and self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE))

    def upsert_chunks(self, chunks: list[ChunkPayload], batch: int = 64) -> int:
        from qdrant_client.models import PointStruct
        import uuid
        n = 0
        for i in range(0, len(chunks), batch):
            part = chunks[i:i + batch]
            vecs = self.embed([c.text for c in part])
            points = [PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, c.chunk_id)),
                vector=v,
                payload=c.model_dump(mode="json"))
                for c, v in zip(part, vecs)]
            self.client.upsert(self.collection, points)
            n += len(points)
        log.info("upserted %d chunks into '%s'", n, self.collection)
        return n

    def upsert_payloads(self, ids: list[str], texts: list[str],
                        payloads: list[dict], batch: int = 64) -> int:
        """Generic upsert for non-ChunkPayload records (e.g. KG node
        embeddings) — same embed-and-write mechanics, arbitrary payload
        dicts instead of a fixed ChunkPayload schema."""
        from qdrant_client.models import PointStruct
        import uuid
        n = 0
        for i in range(0, len(ids), batch):
            id_part = ids[i:i + batch]
            text_part = texts[i:i + batch]
            payload_part = payloads[i:i + batch]
            vecs = self.embed(text_part)
            points = [PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, pid)),
                vector=v, payload=p)
                for pid, v, p in zip(id_part, vecs, payload_part)]
            self.client.upsert(self.collection, points)
            n += len(points)
        log.info("upserted %d payload(s) into '%s'", n, self.collection)
        return n

    # -- retrieval with RBAC pre-filter (FR-6 / ST-402) ----------------------
    def search(self, query: str, role: Role, purpose: Purpose,
               top_k: int = 8, language: Optional[str] = None
               ) -> list[tuple[ChunkPayload, float]]:
        from qdrant_client.models import (FieldCondition, Filter, MatchAny,
                                          MatchValue)
        allowed_sens = [s.value for s in SENSITIVITY_CLEARANCE[role]]
        must = [
            FieldCondition(key="allowed_roles", match=MatchAny(any=[role.value])),
            FieldCondition(key="allowed_purposes", match=MatchAny(any=[purpose.value])),
            FieldCondition(key="sensitivity", match=MatchAny(any=allowed_sens)),
        ]
        if language:
            must.append(FieldCondition(key="language", match=MatchValue(value=language)))
        vec = self.embed([query])[0]
        hits = self.client.query_points(
            self.collection, query=vec, limit=top_k,
            query_filter=Filter(must=must)).points
        out = []
        for h in hits:
            try:
                out.append((ChunkPayload.model_validate(h.payload), float(h.score)))
            except Exception:  # malformed payload — skip, never leak
                log.error("dropping malformed payload id=%s", h.id)
        return out


def bge_m3_embedder() -> tuple[EmbedFn, int]:
    """Default open multilingual embedder (fallback when Sarvam embeddings
    are unavailable). Requires: pip install sentence-transformers

    Requires network access to huggingface.co to download model weights —
    if that's blocked (e.g. this sandbox's egress allowlist doesn't include
    it), this will fail; get_embedder() below catches that and falls back
    further rather than crashing ingestion."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("BAAI/bge-m3")

    def embed(texts: Sequence[str]) -> list[list[float]]:
        return model.encode(list(texts), normalize_embeddings=True).tolist()

    return embed, 1024


def sarvam_embedder(api_key: str, model: str = "sarvam-embed",
                    base_url: str = "https://api.sarvam.ai") -> tuple[EmbedFn, int]:
    """Sarvam embeddings via REST (primary, sovereign path — ST-105).
    Endpoint/model name to be confirmed during key validation."""
    import httpx

    def embed(texts: Sequence[str]) -> list[list[float]]:
        r = httpx.post(f"{base_url}/v1/embeddings",
                       headers={"api-subscription-key": api_key},
                       json={"model": model, "input": list(texts)}, timeout=60)
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]

    probe = embed(["dimension probe"])
    return embed, len(probe[0])


def local_hash_embedder(dim: int = 384) -> tuple[EmbedFn, int]:
    """Deterministic, dependency-free, offline embedder — NOT a real
    semantic embedding model. Hashes word-shingles into a fixed-size
    bag-of-hashed-features vector (the "hashing trick") and L2-normalises
    it, entirely in stdlib (hashlib + math), so it needs zero network
    access and zero model download.

    This exists purely so the embed → write-to-store pipeline can be
    exercised end-to-end (correct dimensions, correct metadata, correct
    Qdrant writes) in environments where NEITHER Sarvam nor Hugging Face
    (needed for BGE-M3) is network-reachable — e.g. this sandbox. Vectors
    from this function carry no real semantic meaning beyond crude lexical
    overlap; do not use it for anything but plumbing verification. Treat
    any manifest built with embedding_model="local-hash-v1" as a
    known-non-semantic index, never a production one.
    """
    import math

    def embed(texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * dim
            words = re.findall(r"\w+", text.lower())
            for w in words:
                h = int(hashlib.sha256(w.encode("utf-8")).hexdigest(), 16)
                idx = h % dim
                sign = 1.0 if (h // dim) % 2 == 0 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    return embed, dim


def get_embedder(provider: Optional[str] = None) -> tuple[EmbedFn, int, str]:
    """Select and wire the actual embedding provider to use, in priority
    order, with an explicit, logged fallback chain rather than a silent
    one — every ingestion run's logs say exactly which embedder ran and
    why, which matters when the resulting index's manifest_id is supposed
    to be an audit trail (manifest.py).

    Order: explicit `provider` arg > $EMBEDDING_PROVIDER env var >
    Sarvam (if SARVAM_API_KEY is set) > BGE-M3 (best-effort — needs
    huggingface.co) > local-hash (last resort, offline, non-semantic —
    see local_hash_embedder()).

    Returns (embed_fn, dim, model_name) — model_name is what gets recorded
    as IndexManifest.embedding_model.
    """
    import os as _os

    choice = provider or _os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
    sarvam_key = _os.getenv("SARVAM_API_KEY", "").strip()

    def _try_sarvam() -> Optional[tuple[EmbedFn, int, str]]:
        if not sarvam_key:
            return None
        try:
            embed, dim = sarvam_embedder(sarvam_key)
            log.info("embedder: using Sarvam embeddings (dim=%d)", dim)
            return embed, dim, "sarvam-embed"
        except Exception as e:
            log.warning("embedder: Sarvam embeddings failed (%s) — falling back", e)
            return None

    def _try_bge_m3() -> Optional[tuple[EmbedFn, int, str]]:
        try:
            embed, dim = bge_m3_embedder()
            log.info("embedder: using BGE-M3 (dim=%d)", dim)
            return embed, dim, "bge-m3"
        except Exception as e:
            log.warning("embedder: BGE-M3 unavailable (%s) — falling back", e)
            return None

    def _local_hash() -> tuple[EmbedFn, int, str]:
        embed, dim = local_hash_embedder()
        log.warning(
            "embedder: falling back to local-hash-v1 — NOT a real semantic "
            "embedder, offline plumbing-verification only (dim=%d). Set "
            "SARVAM_API_KEY or ensure huggingface.co access for a real index.",
            dim)
        return embed, dim, "local-hash-v1"

    if choice == "sarvam":
        return _try_sarvam() or _local_hash()
    if choice in ("bge_m3", "bge-m3"):
        return _try_bge_m3() or _local_hash()
    if choice in ("local_hash", "local-hash"):
        return _local_hash()

    # No explicit choice — try in priority order, degrade gracefully.
    return _try_sarvam() or _try_bge_m3() or _local_hash()
