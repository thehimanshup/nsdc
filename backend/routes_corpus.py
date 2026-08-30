"""Phase 6c — Corpus management API.

These endpoints let an admin operator manage each agent's RAG corpus:

  GET    /api/v1/admin/corpus/stats
                      → counts per agent
  GET    /api/v1/admin/corpus/{agent_id}?q=…&limit=…
                      → list chunks for an agent (filterable by free text)
  GET    /api/v1/admin/corpus/{agent_id}/{chunk_id}
                      → fetch a single chunk
  POST   /api/v1/admin/corpus/{agent_id}/upload
                      multipart upload of .txt / .md / .json / .jsonl
                      → auto-chunked + persisted
  POST   /api/v1/admin/corpus/{agent_id}/chunks
                      manual single-chunk creation (JSON body)
  DELETE /api/v1/admin/corpus/chunks/{chunk_id}
                      remove one chunk
  POST   /api/v1/admin/corpus/test
                      JSON {agent_id, query, k} → preview retrieval
  POST   /api/v1/admin/corpus/reseed
                      wipes EVERY agent's corpus + reseeds from
                      data/personas/seed_corpora.jsonl (demo reset)

Also exposes few-shot management for personas:

  GET    /api/v1/admin/personas/{agent_id}/examples
  POST   /api/v1/admin/personas/{agent_id}/examples
  DELETE /api/v1/admin/personas/{agent_id}/examples/{idx}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import personas
from .agents import get_agent
from .config import settings
from .retrieval import store as chunk_store
from .retrieval.chunk_store import Chunk
from .retrieval.pipeline import (
    delete_chunk, ingest_jsonl, ingest_text, list_chunks_for_agent,
    refresh_agent, retrieve_with_meta,
)

log = logging.getLogger("admin.corpus")
router = APIRouter()


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------

@router.get("/api/v1/admin/corpus/stats")
async def corpus_stats_endpoint() -> dict:
    return {"stats": chunk_store.stats()}


@router.get("/api/v1/admin/corpus/{agent_id}")
async def corpus_list_chunks(agent_id: str,
                              q: str = "",
                              limit: int = Query(default=100, le=500)) -> dict:
    if not get_agent(agent_id):
        raise HTTPException(404, f"agent {agent_id} not found")
    items = list_chunks_for_agent(agent_id, q=q, limit=limit)
    return {
        "agent_id": agent_id,
        "total": len(chunk_store.for_agent(agent_id)),
        "count_returned": len(items),
        "chunks": [_chunk_to_dict(c) for c in items],
    }


@router.get("/api/v1/admin/corpus/{agent_id}/{chunk_id}")
async def corpus_get_chunk(agent_id: str, chunk_id: str) -> dict:
    c = chunk_store.get(chunk_id)
    if not c or c.agent_id != agent_id:
        raise HTTPException(404, "chunk not found")
    return _chunk_to_dict(c)


# ---------------------------------------------------------------------------
# Upload — multipart file → auto-chunked
# ---------------------------------------------------------------------------

@router.post("/api/v1/admin/corpus/{agent_id}/upload")
async def corpus_upload(agent_id: str,
                         file: UploadFile = File(...),
                         source: str = Form(default=""),
                         source_url: str = Form(default=""),
                         last_verified: str = Form(default=""),
                         language: str = Form(default="en-IN"),
                         uploaded_by: str = Form(default="admin")) -> dict:
    """Upload a corpus document. Supported: .txt, .md, .json, .jsonl.

    .jsonl → each line is a fully-structured chunk record (recommended for
             seed packs / re-imports).
    .json  → either a single chunk dict or a list of chunk dicts.
    .txt/.md → auto-chunked at headings (or paragraphs if no headings).
    """
    if not get_agent(agent_id):
        raise HTTPException(404, f"agent {agent_id} not found")

    name = (file.filename or "").lower()
    raw = (await file.read()).decode("utf-8", errors="replace")
    if not raw.strip():
        raise HTTPException(400, "empty file")

    if name.endswith(".jsonl"):
        chunks = ingest_jsonl(agent_id, raw, uploaded_by=uploaded_by)
        mode = "jsonl"
    elif name.endswith(".json"):
        try:
            d = json.loads(raw)
        except Exception as e:
            raise HTTPException(400, f"invalid JSON: {e}")
        if isinstance(d, dict):
            payload = [d]
        elif isinstance(d, list):
            payload = d
        else:
            raise HTTPException(400, "JSON must be an object or an array")
        # Re-serialise as JSONL and reuse the ingest path
        lines = "\n".join(json.dumps(x, ensure_ascii=False) for x in payload)
        chunks = ingest_jsonl(agent_id, lines, uploaded_by=uploaded_by)
        mode = "json"
    elif name.endswith(".txt") or name.endswith(".md") or not name:
        chunks = ingest_text(
            agent_id, raw,
            default_language=language,
            default_source=source or f"upload:{file.filename or 'inline'}",
            default_source_url=source_url,
            default_last_verified=last_verified,
            uploaded_by=uploaded_by,
        )
        mode = "text"
    else:
        raise HTTPException(415, f"unsupported file type: {name}. Use .txt, .md, .json, or .jsonl")

    return {
        "ok": True,
        "mode": mode,
        "ingested": len(chunks),
        "agent_id": agent_id,
        "filename": file.filename,
        "preview": [_chunk_preview(c) for c in chunks[:5]],
    }


# ---------------------------------------------------------------------------
# Manual chunk create
# ---------------------------------------------------------------------------

class ChunkCreate(BaseModel):
    title: str
    body: str
    language: str = "en-IN"
    source: str = ""
    source_url: str = ""
    last_verified: str = ""
    verified_by: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


@router.post("/api/v1/admin/corpus/{agent_id}/chunks")
async def corpus_add_chunk(agent_id: str, req: ChunkCreate) -> dict:
    if not get_agent(agent_id):
        raise HTTPException(404, f"agent {agent_id} not found")
    c = Chunk(
        chunk_id="",
        agent_id=agent_id,
        title=req.title.strip() or "Untitled",
        body=req.body.strip(),
        language=req.language,
        source=req.source,
        source_url=req.source_url,
        last_verified=req.last_verified,
        verified_by=req.verified_by,
        tags=req.tags,
        metadata=req.metadata,
        uploaded_by="admin",
    )
    if not c.body:
        raise HTTPException(400, "body is required")
    chunk_store.add(c)
    refresh_agent(agent_id)
    return {"ok": True, "chunk": _chunk_to_dict(c)}


@router.delete("/api/v1/admin/corpus/chunks/{chunk_id}")
async def corpus_delete_chunk(chunk_id: str) -> dict:
    if not delete_chunk(chunk_id):
        raise HTTPException(404, "chunk not found")
    return {"ok": True, "deleted": chunk_id}


# ---------------------------------------------------------------------------
# Test retrieval
# ---------------------------------------------------------------------------

class CorpusTestRequest(BaseModel):
    agent_id: str
    query: str
    k: int = 5
    include_cross: bool = True


@router.post("/api/v1/admin/corpus/test")
async def corpus_test_retrieval(req: CorpusTestRequest) -> dict:
    a = get_agent(req.agent_id)
    if not a:
        raise HTTPException(404, f"agent {req.agent_id} not found")
    extra = a.cross_corpus_read if (req.include_cross and a.cross_corpus_read) else None
    results = retrieve_with_meta(req.agent_id, req.query, k=req.k, extra_corpora=extra)
    return {
        "ok": True,
        "query": req.query,
        "agent_id": req.agent_id,
        "cross_corpus_read": extra or [],
        "hits": [
            {
                "score": round(s, 4),
                "chunk": _chunk_preview(c),
                "source": c.source,
                "last_verified": c.last_verified,
                "from_agent": c.agent_id,
            }
            for c, s in results
        ],
    }


# ---------------------------------------------------------------------------
# Reseed (demo reset)
# ---------------------------------------------------------------------------

@router.post("/api/v1/admin/corpus/reseed")
async def corpus_reseed() -> dict:
    """Wipe every agent's corpus and re-ingest from seed_corpora.jsonl."""
    if not settings.allow_demo_routes:
        raise HTTPException(403, "corpus reseed is disabled in production")
    seed = Path(settings.data_dir) / "personas" / "seed_corpora.jsonl"
    if not seed.exists():
        seed = Path(__file__).resolve().parent.parent / "data" / "personas" / "seed_corpora.jsonl"
    if not seed.exists():
        raise HTTPException(500, "no seed file found")
    # Wipe all existing
    for aid in list(chunk_store.all_agent_ids()):
        chunk_store.replace_all_for_agent(aid, [])
    raw = seed.read_text(encoding="utf-8")
    chunks = ingest_jsonl("__seed__", raw, uploaded_by="reseed")
    return {"ok": True, "ingested": len(chunks),
            "stats": chunk_store.stats()}


# ---------------------------------------------------------------------------
# Few-shot example management (personas)
# ---------------------------------------------------------------------------

class ExampleCreate(BaseModel):
    user: str
    agent: str
    language: str = "en-IN"
    tags: list[str] = Field(default_factory=list)


@router.get("/api/v1/admin/personas/{agent_id}/examples")
async def personas_list(agent_id: str) -> dict:
    if not get_agent(agent_id):
        raise HTTPException(404, "agent not found")
    return {"agent_id": agent_id, "examples": personas.list_examples(agent_id)}


@router.post("/api/v1/admin/personas/{agent_id}/examples")
async def personas_add(agent_id: str, req: ExampleCreate) -> dict:
    if not get_agent(agent_id):
        raise HTTPException(404, "agent not found")
    ex = personas.add_example(agent_id, req.user, req.agent,
                               language=req.language, tags=req.tags)
    return {"ok": True, "example": ex,
            "total": len(personas.list_examples(agent_id))}


@router.delete("/api/v1/admin/personas/{agent_id}/examples/{idx}")
async def personas_delete(agent_id: str, idx: int) -> dict:
    if not personas.delete_example(agent_id, idx):
        raise HTTPException(404, "example not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk_to_dict(c: Chunk) -> dict:
    d = c.to_dict()
    # Trim body for the list view to keep payload small
    if len(d.get("body", "")) > 400:
        d["body_preview"] = d["body"][:400] + "…"
    return d


def _chunk_preview(c: Chunk) -> dict:
    return {
        "chunk_id": c.chunk_id,
        "title": c.title,
        "body": (c.body[:280] + "…") if len(c.body) > 280 else c.body,
        "agent_id": c.agent_id,
        "language": c.language,
        "tags": c.tags,
        "source": c.source,
        "last_verified": c.last_verified,
    }
