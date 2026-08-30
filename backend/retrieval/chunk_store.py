"""Chunk dataclass + persistence layer.

One JSONL file per agent at `data/corpora/{agent_id}/chunks.jsonl`.
Append-only writes for safe ingestion; reads via in-memory index.

Each chunk is a self-contained snippet of departmental knowledge that
the LLM can quote in its response. Rich metadata supports citations
and metadata-aware boosting at retrieval time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Optional

from ..config import settings

log = logging.getLogger("retrieval.store")


@dataclass
class Chunk:
    """A single retrievable knowledge snippet."""
    agent_id: str = ""                         # owning agent (set by ingest pipeline)
    title: str = ""                            # short heading shown in citations
    body: str = ""                             # the actual text the LLM reads
    chunk_id: str = ""                         # unique id (auto-generated if blank)
    language: str = "en-IN"                    # primary language
    source: str = ""                           # e.g. "G.O. Ms. 78 of 12 Mar 2026"
    source_url: str = ""                       # citation URL
    last_verified: str = ""                    # YYYY-MM-DD
    verified_by: str = ""                      # officer designation
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)   # scheme_id, eligibility, etc.
    uploaded_at: str = ""                      # when ingested
    uploaded_by: str = ""                      # admin user / "seed" / "system"
    # Phase 6d — state scoping. "central" for nationwide schemes
    # (PM-KISAN, Ayushman Bharat, DigiLocker), otherwise a 2-letter
    # state code (TN, KA, MH, …). The retrieval pipeline filters by
    # citizen's state OR "central".
    state_code: str = "central"

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def to_context_block(self) -> str:
        """Render as a system-prompt context block with citation marker."""
        cite = self.citation_marker()
        meta = self._inline_metadata_block()
        return (
            f"--- {self.title} ---\n"
            f"{self.body.strip()}\n"
            f"{meta}"
            f"[SOURCE: {cite}]"
        )

    def _inline_metadata_block(self) -> str:
        """Pull the most-useful metadata into the LLM context."""
        bits = []
        m = self.metadata or {}
        for key in ("scheme_id", "amount", "eligibility", "documents_required",
                    "process", "helpline", "office"):
            v = m.get(key)
            if v:
                if isinstance(v, (list, tuple)):
                    v = "; ".join(str(x) for x in v)
                bits.append(f"  {key}: {v}")
        if self.last_verified:
            bits.append(f"  last_verified: {self.last_verified}")
        if bits:
            return "\n".join(bits) + "\n"
        return ""

    def citation_marker(self) -> str:
        parts = [self.source or self.title]
        if self.last_verified:
            parts.append(f"verified {self.last_verified}")
        return " | ".join(parts)

    def as_citation(self) -> dict:
        """Compact citation payload for the simulator/admin UI."""
        return {
            "chunkId": self.chunk_id,
            "title": self.title,
            "source": self.source,
            "sourceUrl": self.source_url,
            "lastVerified": self.last_verified,
            "verifiedBy": self.verified_by,
            "agentId": self.agent_id,
            "language": self.language,
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        # Skip unknown keys for forward-compat
        from dataclasses import fields
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Persistence — one JSONL file per agent
# ---------------------------------------------------------------------------

class ChunkStore:
    """In-memory cache backed by JSONL on disk."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_agent: dict[str, list[Chunk]] = {}
        self._by_id: dict[str, Chunk] = {}

    # ----- paths -----
    def _agent_dir(self, agent_id: str) -> Path:
        d = Path(settings.data_dir) / "corpora" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _chunks_file(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "chunks.jsonl"

    # ----- bulk load -----
    def load_all(self) -> int:
        """Read every agent's JSONL file. Returns total chunks loaded."""
        root = Path(settings.data_dir) / "corpora"
        if not root.exists():
            return 0
        with self._lock:
            self._by_agent.clear()
            self._by_id.clear()
            total = 0
            for agent_dir in sorted(root.iterdir()):
                if not agent_dir.is_dir():
                    continue
                f = agent_dir / "chunks.jsonl"
                if not f.exists():
                    continue
                count_this = 0
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        c = Chunk.from_dict(json.loads(line))
                    except Exception as e:
                        log.warning("Bad chunk line in %s: %s", f, e)
                        continue
                    self._by_agent.setdefault(c.agent_id, []).append(c)
                    self._by_id[c.chunk_id] = c
                    count_this += 1
                total += count_this
                log.info("Loaded %d chunks for agent %s", count_this, agent_dir.name)
            return total

    # ----- read -----
    def for_agent(self, agent_id: str) -> list[Chunk]:
        with self._lock:
            return list(self._by_agent.get(agent_id, []))

    def get(self, chunk_id: str) -> Optional[Chunk]:
        with self._lock:
            return self._by_id.get(chunk_id)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._by_agent.items()}

    def all_agent_ids(self) -> list[str]:
        with self._lock:
            return list(self._by_agent.keys())

    # ----- write -----
    def add(self, chunk: Chunk) -> Chunk:
        """Append a single chunk; persist to JSONL."""
        with self._lock:
            if not chunk.chunk_id:
                chunk.chunk_id = self._make_id(chunk)
            if not chunk.uploaded_at:
                chunk.uploaded_at = _now_iso()
            self._by_agent.setdefault(chunk.agent_id, []).append(chunk)
            self._by_id[chunk.chunk_id] = chunk
            with open(self._chunks_file(chunk.agent_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        return chunk

    def add_many(self, chunks: list[Chunk]) -> list[Chunk]:
        return [self.add(c) for c in chunks]

    def delete(self, chunk_id: str) -> bool:
        """Remove a chunk and rewrite that agent's JSONL atomically."""
        with self._lock:
            c = self._by_id.pop(chunk_id, None)
            if not c:
                return False
            siblings = self._by_agent.get(c.agent_id, [])
            self._by_agent[c.agent_id] = [x for x in siblings if x.chunk_id != chunk_id]
            self._rewrite_agent_file(c.agent_id)
        return True

    def replace_all_for_agent(self, agent_id: str, chunks: list[Chunk]) -> None:
        """Used by seed-import to atomically replace an agent's corpus."""
        with self._lock:
            # Remove the agent's existing chunks from _by_id
            for old in self._by_agent.get(agent_id, []):
                self._by_id.pop(old.chunk_id, None)
            self._by_agent[agent_id] = []
            for c in chunks:
                if not c.chunk_id:
                    c.chunk_id = self._make_id(c)
                if not c.uploaded_at:
                    c.uploaded_at = _now_iso()
                self._by_agent[agent_id].append(c)
                self._by_id[c.chunk_id] = c
            self._rewrite_agent_file(agent_id)

    def _rewrite_agent_file(self, agent_id: str) -> None:
        path = self._chunks_file(agent_id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for c in self._by_agent.get(agent_id, []):
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(path)

    def _make_id(self, chunk: Chunk) -> str:
        """Deterministic-ish id so re-ingestion doesn't duplicate the same chunk."""
        seed = f"{chunk.agent_id}|{chunk.title}|{chunk.body[:64]}"
        h = hashlib.sha256(seed.encode()).hexdigest()[:10]
        return f"chk_{chunk.agent_id[:6]}_{h}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Module-level singleton
store = ChunkStore()
