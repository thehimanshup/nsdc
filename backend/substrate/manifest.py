"""Deterministic index manifests — RFP 4.B.1(c), PRD FR-4 (ST-204).

Every index build produces a manifest capturing exactly what went into it:
embedding model, chunking configuration, corpus snapshot, KG version.
Every answer records the manifest ID it was served from, so any response
can be traced back to the precise index state that produced it.

Manifests are content-addressed: the manifest_id is derived from the
manifest body, so identical inputs always produce the same ID
(deterministic rebuild verification).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, Field

MANIFEST_DIR_NAME = "manifests"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_hash(obj) -> str:
    """Stable hash of any JSON-serialisable object."""
    return _sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8"))


class ChunkingConfig(BaseModel):
    strategy: str = "section_aware"      # section_aware | fixed
    target_tokens: int = 400
    overlap_tokens: int = 60
    keep_tables_intact: bool = True
    ocr_enabled: bool = True             # route legacy/scanned sources through Sarvam Vision
    dedup_enabled: bool = True           # drop exact-duplicate chunks, flag near-duplicates

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.model_dump())


class IndexManifest(BaseModel):
    manifest_id: str = ""                # filled by finalise()
    embedding_model: str                 # e.g. "bge-m3" or "sarvam-embed-v1"
    embedding_dim: int
    chunking_config: ChunkingConfig
    chunking_config_hash: str = ""
    corpus_snapshot_hash: str = ""       # hash over sorted chunk_hashes
    doc_count: int = 0
    chunk_count: int = 0
    kg_version: str = ""                 # KG release tag (ST-303)
    kg_content_hash: str = ""
    kg_node_count: int = 0                # structured KG-node embeddings written alongside chunks
    vector_store_backend: str = "qdrant"     # e.g. "qdrant" | "pgvector"
    store_schema_version: str = "1"          # bump on breaking payload-schema changes
    built_at: str = ""
    schema_version: str = "0.1"

    def finalise(self, chunk_hashes: Iterable[str]) -> "IndexManifest":
        """Compute derived fields. Call once, after ingestion, before save.

        kg_node_count is deliberately excluded from the hashed body: it's
        only known after a separate KG-node embedding pass that runs after
        this manifest_id is computed (see ingest.run()) — including it here
        would let the same manifest_id end up pointing at different
        kg_node_count values depending on run order, exactly the kind of
        drift this content-addressing scheme exists to prevent.
        """
        hashes = sorted(chunk_hashes)
        self.chunk_count = len(hashes)
        self.corpus_snapshot_hash = canonical_hash(hashes)
        self.chunking_config_hash = self.chunking_config.config_hash
        self.built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = self.model_dump(exclude={"manifest_id", "built_at", "kg_node_count"})
        self.manifest_id = "man-" + canonical_hash(body)[7:19]
        return self


class ManifestRegistry:
    """File-backed registry (data/manifests/*.json), surfaced in the
    Console version-registry view (ST-1104)."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir) / MANIFEST_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, manifest: IndexManifest) -> Path:
        if not manifest.manifest_id:
            raise ValueError("manifest not finalised — call finalise() first")
        path = self.dir / f"{manifest.manifest_id}.json"
        if path.exists():
            # Immutability guard: a manifest_id is content-addressed, so a
            # file already on disk under that ID must describe the same
            # index state. Identical re-save is a harmless no-op (idempotent
            # re-runs); divergent content under the same ID means something
            # tampered with the hashed fields after finalise() — refuse.
            existing = IndexManifest.model_validate_json(
                path.read_text(encoding="utf-8"))
            mutable = {"built_at", "kg_node_count"}   # excluded from the hash
            if existing.model_dump(exclude=mutable) != manifest.model_dump(exclude=mutable):
                raise ValueError(
                    f"manifest {manifest.manifest_id} already exists with "
                    "different content — manifests are immutable; changed "
                    "inputs must produce a new finalise()d manifest_id")
        else:
            path.write_text(manifest.model_dump_json(indent=1), encoding="utf-8")
        (self.dir / "CURRENT").write_text(manifest.manifest_id, encoding="utf-8")
        return path

    def current_id(self) -> Optional[str]:
        p = self.dir / "CURRENT"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def load(self, manifest_id: str) -> IndexManifest:
        return IndexManifest.model_validate_json(
            (self.dir / f"{manifest_id}.json").read_text(encoding="utf-8"))

    def list_all(self) -> list[IndexManifest]:
        return sorted((self.load(p.stem) for p in self.dir.glob("man-*.json")),
                      key=lambda m: m.built_at, reverse=True)
