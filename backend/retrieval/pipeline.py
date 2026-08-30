"""High-level retrieval API + ingestion pipeline.

Two main entry points:

    retrieve(agent_id, query, k=3, extra_corpora=None) -> [Chunk]
    ingest_text(agent_id, text, default_meta=None) -> [Chunk]

The pipeline supports:

  - Cross-corpus retrieval (Health may pull from CMO's corpus if the
    agent has cross_corpus_read=["cmo"]).
  - Auto-chunking of free-form .txt / .md uploads at headings
    ("# ", "## ", "### " or blank-line paragraphs).
  - JSONL ingestion (preferred) — each line is a fully-structured
    chunk record.
  - In-process index cache that's rebuilt incrementally when chunks
    are added/deleted.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from threading import RLock
from typing import Iterable, Optional

from .bm25 import BM25Index, tokenize
from .chunk_store import Chunk, store

log = logging.getLogger("retrieval.pipeline")

_LOCK = RLock()
_INDEX_CACHE: dict[str, BM25Index] = {}


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------

def build_indices() -> int:
    """Build a BM25 index per agent from the current ChunkStore contents."""
    with _LOCK:
        _INDEX_CACHE.clear()
        for aid in store.all_agent_ids():
            chunks = store.for_agent(aid)
            _INDEX_CACHE[aid] = BM25Index(chunks)
        return len(_INDEX_CACHE)


def refresh_agent(agent_id: str) -> None:
    """Rebuild the index for a single agent after a mutation."""
    with _LOCK:
        chunks = store.for_agent(agent_id)
        _INDEX_CACHE[agent_id] = BM25Index(chunks)


def _ensure_index(agent_id: str) -> Optional[BM25Index]:
    with _LOCK:
        if agent_id not in _INDEX_CACHE:
            chunks = store.for_agent(agent_id)
            if not chunks:
                return None
            _INDEX_CACHE[agent_id] = BM25Index(chunks)
        return _INDEX_CACHE[agent_id]


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def retrieve(agent_id: str, query: str, k: int = 3,
              extra_corpora: Optional[list[str]] = None,
              state_code: str = "") -> list[Chunk]:
    """Return the top-k chunks; just the chunks (no scores).

    Walks the agent's own index plus any `extra_corpora` (cross-corpus
    reads from the Agent.cross_corpus_read allow-list). Results from
    cross-corpus reads have their score down-weighted by 0.7.

    Phase 6d: `state_code` filters to chunks whose state_code matches
    the citizen's state OR is "central". An empty state_code = no filter.
    """
    return [c for c, _ in retrieve_with_meta(agent_id, query, k=k,
                                              extra_corpora=extra_corpora,
                                              state_code=state_code)]


def retrieve_with_meta(agent_id: str, query: str, k: int = 3,
                        extra_corpora: Optional[list[str]] = None,
                        state_code: str = "",
                        ) -> list[tuple[Chunk, float]]:
    """Return [(chunk, score), …] including cross-corpus + state-filtered hits."""
    all_hits: list[tuple[Chunk, float]] = []
    idx = _ensure_index(agent_id)
    # Retrieve 4x what we need so the state filter has room to discard
    bigger_k = k * 4 if state_code else k * 2
    if idx:
        all_hits.extend(idx.search(query, k=bigger_k))
    cross_weight = 0.7
    for cross in (extra_corpora or []):
        if cross == agent_id:
            continue
        cidx = _ensure_index(cross)
        if not cidx:
            continue
        for c, s in cidx.search(query, k=max(1, k // 2)):
            all_hits.append((c, s * cross_weight))

    # Phase 6d — state filter: keep chunks whose state_code matches the
    # citizen's state, or is "central" (nationwide schemes are visible
    # to every citizen).
    if state_code:
        sc_upper = state_code.upper()
        all_hits = [(c, s) for c, s in all_hits
                     if (c.state_code or "central").upper() in (sc_upper, "CENTRAL")]

    all_hits.sort(key=lambda cs: cs[1], reverse=True)
    return all_hits[:k]


def corpus_stats() -> dict[str, int]:
    return store.stats()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

# Heuristic split: ### Heading, ## Heading, # Heading, then double-newline.
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def ingest_text(agent_id: str, text: str,
                 *,
                 default_language: str = "en-IN",
                 default_source: str = "",
                 default_source_url: str = "",
                 default_last_verified: str = "",
                 uploaded_by: str = "admin",
                 ) -> list[Chunk]:
    """Auto-chunk free-form text and persist. Returns the chunks created.

    Splits on Markdown headings (#, ##, ###). If the text has no headings,
    splits on blank lines into paragraph chunks. Title is taken from the
    heading line; if no heading, the first 60 chars become the title.
    """
    blocks = _split_into_blocks(text)
    out: list[Chunk] = []
    for title, body in blocks:
        if not body.strip():
            continue
        c = Chunk(
            chunk_id="",
            agent_id=agent_id,
            title=title or _auto_title(body),
            body=body.strip(),
            language=default_language,
            source=default_source,
            source_url=default_source_url,
            last_verified=default_last_verified,
            uploaded_by=uploaded_by,
        )
        store.add(c)
        out.append(c)
    if out:
        refresh_agent(agent_id)
    log.info("Ingested %d chunks for %s (text mode)", len(out), agent_id)
    return out


def ingest_jsonl(agent_id: str, jsonl: str,
                  *, uploaded_by: str = "admin") -> list[Chunk]:
    """Each line of `jsonl` is a JSON object with chunk fields.

    `agent_id` may be overridden per-line via the line's "agent_id" field.
    Useful when uploading a multi-department seed corpus.
    """
    out: list[Chunk] = []
    bad = 0
    touched: set[str] = set()
    for raw in jsonl.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            d = json.loads(raw)
        except Exception as e:
            log.warning("Skipping bad JSONL line: %s", e)
            bad += 1
            continue
        d.setdefault("agent_id", agent_id)
        d.setdefault("uploaded_by", uploaded_by)
        # Required fields fallback
        d.setdefault("title", d.get("metadata", {}).get("scheme_id", "untitled"))
        d.setdefault("body", "")
        if not d["body"]:
            bad += 1
            continue
        c = store.add(Chunk.from_dict(d))
        out.append(c)
        touched.add(c.agent_id)
    for aid in touched:
        refresh_agent(aid)
    log.info("Ingested %d chunks (%d skipped) for %s (jsonl mode)",
             len(out), bad, agent_id)
    return out


def _split_into_blocks(text: str) -> list[tuple[str, str]]:
    """Return list of (title, body) pairs for `text`.

    Strategy:
      1. If the text has any Markdown headings, split there
      2. Else split on blank lines (paragraph-level)
      3. Else a single block
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n")
    matches = list(_HEADING_RE.finditer(text))
    if matches:
        blocks: list[tuple[str, str]] = []
        for i, m in enumerate(matches):
            title = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            blocks.append((title, body))
        # Anything before the first heading goes in as an unnamed preamble
        pre = text[:matches[0].start()].strip()
        if pre:
            blocks.insert(0, (_auto_title(pre), pre))
        return blocks

    # No headings → paragraph split
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) == 1:
        return [(_auto_title(paras[0]), paras[0])]
    return [(_auto_title(p), p) for p in paras]


def _auto_title(body: str) -> str:
    first_line = body.split("\n", 1)[0].strip()
    return first_line[:60] + ("…" if len(first_line) > 60 else "")


# ---------------------------------------------------------------------------
# Mutation API used by admin routes
# ---------------------------------------------------------------------------

def delete_chunk(chunk_id: str) -> bool:
    c = store.get(chunk_id)
    if not c:
        return False
    ok = store.delete(chunk_id)
    if ok:
        refresh_agent(c.agent_id)
    return ok


def list_chunks_for_agent(agent_id: str, *, q: str = "", limit: int = 200) -> list[Chunk]:
    items = store.for_agent(agent_id)
    if q:
        ql = q.lower()
        items = [c for c in items if ql in c.title.lower()
                 or ql in c.body.lower()
                 or ql in (c.source or "").lower()
                 or any(ql in t.lower() for t in (c.tags or []))]
    return items[:limit]


def get_chunk(chunk_id: str) -> Optional[Chunk]:
    return store.get(chunk_id)
