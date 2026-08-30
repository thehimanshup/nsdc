"""Structured (KG-node) embeddings — ST-Embedding-Generation.

Renders each Knowledge-Graph node (from backend/substrate/kg/curated/*.csv
— the same curated source backend.substrate.kg.loader loads into Neo4j)
into an embeddable text form, embeds it, and writes it to a *separate*
Qdrant collection from the unstructured chunk corpus.

Deliberately does NOT require a live Neo4j connection: the curated CSVs
are already the source of truth for node content: reading them here means
structured embeddings can be produced any time and don't go stale relative
to a KG load that hasn't happened yet in this environment.

Usage:
    python -m backend.substrate.kg_embed              # embed + write
    python -m backend.substrate.kg_embed --provider sarvam
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

from .kg.loader import CURATED, content_hash
from .schemas import KGNodePayload

log = logging.getLogger("substrate.kg_embed")

KG_COLLECTION = "skilling_kg_nodes"


def _rows(name: str) -> list[dict]:
    p = CURATED / name
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        # DictReader stores any extra columns on a ragged row under a
        # `None` key (its `restkey` default) — strip that before it reaches
        # KGNodePayload.attrs (dict[str, Any], which rejects a non-str key).
        return [{k: v for k, v in r.items() if k is not None}
                for r in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# One render function per node type — mirrors the CYPHER_BATCHES table in
# kg/loader.py (same CSVs, same node types), but produces natural-language
# text suitable for embedding rather than a Cypher MERGE statement.
# ---------------------------------------------------------------------------

def _render_qp(r: dict) -> KGNodePayload:
    text = (f"Qualification Pack {r['qp_code']}: {r['title']}. "
            f"NSQF Level {r['nsqf_level']}, version {r['version']}.")
    return KGNodePayload(node_id=f"qp:{r['qp_code']}", node_type="QualificationPack",
                         label=r["title"], text=text,
                         source_doc_id=r.get("source_doc_id") or None, attrs=r)


def _render_nos(r: dict) -> KGNodePayload:
    text = (f"National Occupational Standard {r['nos_code']}: {r['title']} "
            f"(under Qualification Pack {r['qp_code']}).")
    return KGNodePayload(node_id=f"nos:{r['nos_code']}", node_type="NOS",
                         label=r["title"], text=text,
                         source_doc_id=r.get("source_doc_id") or None, attrs=r)


def _render_skill(r: dict) -> KGNodePayload:
    text = f"Skill: {r['label']} (required by {r['nos_code']})."
    return KGNodePayload(node_id=f"skill:{r['id']}", node_type="Skill",
                         label=r["label"], text=text, attrs=r)


def _render_jobrole(r: dict) -> KGNodePayload:
    text = f"Job Role: {r['title']}, maps to Qualification Pack {r['qp_code']}."
    if r.get("esco_id"):
        text += f" ESCO crosswalk: {r['esco_id']}."
    if r.get("onet_id"):
        text += f" O*NET crosswalk: {r['onet_id']}."
    return KGNodePayload(node_id=f"jobrole:{r['id']}", node_type="JobRole",
                         label=r["title"], text=text, attrs=r)


def _render_course(r: dict) -> KGNodePayload:
    text = (f"Course: {r['title']}, covers Qualification Pack {r['covers_qp']}. "
            f"Duration {r['duration_hours']} hours, mode: {r['mode']}.")
    return KGNodePayload(node_id=f"course:{r['id']}", node_type="Course",
                         label=r["title"], text=text,
                         source_doc_id=r.get("source_doc_id") or None, attrs=r)


def _render_centre(r: dict) -> KGNodePayload:
    text = f"Training Centre: {r['name']}, {r['district']}, {r['state']}."
    return KGNodePayload(node_id=f"centre:{r['id']}", node_type="TrainingCentre",
                         label=r["name"], text=text, attrs=r)


def _render_scheme_group(rows: list[dict]) -> KGNodePayload:
    """A scheme can have one row per course it supports (nodes_scheme.csv is
    an edge list, not a one-row-per-entity table) — group by id first so
    multiple SUPPORTS relationships all end up in one node's text instead of
    later rows silently overwriting earlier ones at the same node_id."""
    first = rows[0]
    courses = [r["supports_course"] for r in rows if r.get("supports_course")]
    text = f"Scheme: {first['name']}, supports course(s): {', '.join(courses)}."
    return KGNodePayload(node_id=f"scheme:{first['id']}", node_type="Scheme",
                         label=first["name"], text=text,
                         attrs={**first, "supports_courses": courses})


def _render_eligibility_rule(r: dict) -> KGNodePayload:
    text = f"Eligibility rule for {r['scheme_id']}: {r['label']} ({r['criterion']} {r['op']} {r['value']})."
    return KGNodePayload(node_id=f"rule:{r['id']}", node_type="EligibilityRule",
                         label=r["label"], text=text, attrs=r)


def _render_xwalk(r: dict) -> KGNodePayload:
    text = f"External occupation crosswalk ({r['scheme']}): {r['label']}."
    return KGNodePayload(node_id=f"xwalk:{r['id']}", node_type="ExternalOccupation",
                         label=r["label"], text=text, attrs=r)


_RENDERERS = [
    ("nodes_qp.csv", _render_qp),
    ("nodes_nos.csv", _render_nos),
    ("nodes_skill.csv", _render_skill),
    ("nodes_jobrole.csv", _render_jobrole),
    ("nodes_course.csv", _render_course),
    ("nodes_centre.csv", _render_centre),
    ("rules_eligibility.csv", _render_eligibility_rule),
    ("xwalk_external.csv", _render_xwalk),
]


def load_kg_node_records() -> list[KGNodePayload]:
    """Render every curated KG node into an embeddable KGNodePayload.
    Deterministic given the CSVs — same rows always produce the same
    node_id/text (no timestamps, no randomness)."""
    hash_now = content_hash()
    out: list[KGNodePayload] = []
    for csv_name, renderer in _RENDERERS:
        for row in _rows(csv_name):
            node = renderer(row)
            node.kg_content_hash = hash_now
            out.append(node)

    # nodes_scheme.csv is an edge list (one row per course a scheme
    # supports) rather than one-row-per-entity — group by id first so a
    # scheme supporting N courses produces one node with all N courses in
    # its text, not N nodes silently colliding on the same node_id.
    by_scheme_id: dict[str, list[dict]] = {}
    for row in _rows("nodes_scheme.csv"):
        by_scheme_id.setdefault(row["id"], []).append(row)
    for rows in by_scheme_id.values():
        node = _render_scheme_group(rows)
        node.kg_content_hash = hash_now
        out.append(node)

    return out


def embed_and_store_kg_nodes(vs, manifest_id: str = "") -> int:
    """Embed every KG node's rendered text and write it to `vs` (a
    VectorStore-like object already pointed at the KG_COLLECTION). Returns
    the number of nodes written."""
    nodes = load_kg_node_records()
    if not nodes:
        log.warning("no KG node records found under %s — nothing to embed", CURATED)
        return 0
    for n in nodes:
        n.index_manifest_id = manifest_id
    ids = [n.node_id for n in nodes]
    texts = [n.text for n in nodes]
    payloads = [n.model_dump(mode="json") for n in nodes]
    written = vs.upsert_payloads(ids, texts, payloads)
    log.info("wrote %d KG-node embeddings to '%s'", written, KG_COLLECTION)
    return written


# ---------------------------------------------------------------------- main
def run(qdrant_path: Optional[str] = None, qdrant_url: Optional[str] = None,
        provider: Optional[str] = None) -> int:
    from .vector_store import VectorStore, get_embedder

    embed, dim, model_name = get_embedder(provider)
    vs = VectorStore(embed, dim, url=qdrant_url, path=qdrant_path,
                     collection=KG_COLLECTION)
    vs.ensure_collection()
    written = embed_and_store_kg_nodes(vs)
    log.info("KG-node embedding run complete: %d nodes, model=%s, dim=%d",
             written, model_name, dim)
    return written


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--qdrant-path", default="data/qdrant_local")
    p.add_argument("--qdrant-url", default=None)
    p.add_argument("--provider", default=None)
    args = p.parse_args()
    run(qdrant_path=args.qdrant_path if not args.qdrant_url else None,
       qdrant_url=args.qdrant_url, provider=args.provider)
