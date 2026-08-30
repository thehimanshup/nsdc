"""Hybrid retriever — vector + BM25 + KG fusion (ST-403/404, RFP 4.B.1d).

Three legs, all RBAC-filtered, fused with Reciprocal Rank Fusion:

  vector   VectorStore.search()             (semantic, filtered in-store)
  bm25     phase6e backend.retrieval        (keyword — REUSED, filtered here
                                             post-hoc with the same predicate
                                             the vector store applies in-store)
  kg       Neo4j pathway traversal          (for `pathway` intents)

Output is an EvidenceBundle for gates.evidence_gate().
"""
from __future__ import annotations

import logging
from typing import Optional

from .gates import EvidenceBundle
from .schemas import ChunkPayload, Purpose, Role, SENSITIVITY_CLEARANCE
from .vector_store import VectorStore

log = logging.getLogger("substrate.retriever")

RRF_K = 60  # standard reciprocal-rank-fusion constant

# Generic skilling words that appear everywhere in the corpus — excluded from
# the coverage signal so they don't mask out-of-corpus queries.
_GENERIC = {"course", "courses", "fees", "fee", "training", "skill", "skills",
            "which", "should", "about", "tell", "want", "take", "what",
            "scheme", "india", "certificate", "month", "months", "report",
            "show", "list", "कोर्स", "कौन", "क्या", "चाहिए",
            # english stopwords ≥4 chars (would otherwise match any document)
            "this", "that", "these", "those", "with", "from", "have", "does",
            "will", "your", "when", "where", "been", "being", "there", "their",
            "them", "they", "than", "then", "some", "such", "only", "into",
            "over", "under", "would", "could", "also", "more", "most", "much",
            "many", "need", "needs", "please", "give", "know", "like", "help"}


def _stem(t: str) -> str:
    """Light suffix-stripping so 'passed'/'passing' match 'pass' in corpus."""
    for suf in ("ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def _informative_terms(query: str) -> list[str]:
    toks = [t.lower() for t in
            __import__("re").findall(r"[\wऀ-ॿ]+", query)]
    return [_stem(t) for t in toks if len(t) >= 4 and t not in _GENERIC]


def focus_query(query: str) -> str:
    """Query rewritten to the informative terms + code-like tokens (GDA,
    HSS/Q5101, NSQF…). Used as a second BM25 pass so generic words like
    'course' can't crowd the topically relevant chunks out of the top-k.
    Returns '' when nothing informative remains."""
    import re
    toks = re.findall(r"[\w/ऀ-ॿ]+", query)
    keep = []
    for t in toks:
        tl = t.lower()
        is_code = bool(re.search(r"\d", t)) or "/" in t or (t.isupper() and len(t) >= 2)
        if is_code or (len(tl) >= 4 and tl not in _GENERIC):
            keep.append(t)
    return " ".join(keep)


def _rbac_ok(chunk: ChunkPayload, role: Role, purpose: Purpose) -> bool:
    """The single RBAC predicate — identical semantics to the in-store
    filter in vector_store.search(). Applied post-hoc to legs that cannot
    filter natively (BM25). Fail closed on any mismatch."""
    return (role in chunk.allowed_roles
            and purpose in chunk.allowed_purposes
            and chunk.sensitivity in SENSITIVITY_CLEARANCE[role])


class HybridRetriever:
    def __init__(self, vector_store: VectorStore,
                 bm25_retrieve=None,       # phase6e: backend.retrieval.retrieve
                 kg_session_factory=None): # neo4j session factory (optional)
        self.vs = vector_store
        self.bm25_retrieve = bm25_retrieve
        self.kg_session_factory = kg_session_factory

    # ------------------------------------------------------------------ legs
    def _vector_leg(self, query: str, role: Role, purpose: Purpose,
                    k: int) -> list[tuple[ChunkPayload, float]]:
        if self.vs is None:
            return []
        return self.vs.search(query, role, purpose, top_k=k)

    def _bm25_leg(self, query: str, role: Role, purpose: Purpose,
                  agent_id: str, k: int) -> list[tuple[ChunkPayload, float]]:
        if self.bm25_retrieve is None:
            return []
        raw = self.bm25_retrieve(agent_id, query, k=k * 2)  # over-fetch, filter
        out: list[tuple[ChunkPayload, float]] = []
        for item in raw:
            ch, raw_score = item if isinstance(item, tuple) else (item, 0.0)
            try:
                payload = ChunkPayload.model_validate(
                    ch if isinstance(ch, dict) else ch.__dict__ | {"text": ch.text})
            except Exception:
                continue  # legacy chunk without governance metadata → drop
            if _rbac_ok(payload, role, purpose):
                out.append((payload, raw_score))
            if len(out) >= k:
                break
        # Normalise BM25 scores (unbounded) into the 0..0.75 evidence band,
        # weighted by query-term coverage. Pure rank-normalisation would make
        # the best hit look strong even for out-of-corpus queries (breaking
        # the evidence gate / refusal behaviour); coverage of the query's
        # informative terms restores an absolute relevance signal until the
        # vector leg takes over this job.
        top = max((s for _, s in out), default=0.0)
        if top > 0:
            terms = _informative_terms(query)
            scored = []
            for c, s in out:
                text_lower = c.text.lower()
                cov = (sum(1 for t in terms if t in text_lower) / len(terms)
                       if terms else 1.0)
                # Steep coverage weighting: a chunk matching only a third of
                # the query's informative terms (e.g. 'technician' from
                # 'Advanced Robotics Technician') must NOT clear the evidence
                # gate; a full-subject match must clear it comfortably.
                scored.append((c, (0.35 + 0.40 * (s / top)) * (0.15 + 0.85 * cov)))
            out = scored
        return out

    def _kg_leg(self, goal: str) -> tuple[list[str], list[list[str]]]:
        if self.kg_session_factory is None:
            return [], []
        from .kg.loader import PATHWAY_QUERY
        node_ids: list[str] = []
        paths: list[list[str]] = []
        with self.kg_session_factory() as session:
            for rec in session.run(PATHWAY_QUERY, goal=goal):
                path = ([f"jobrole:{rec['job_role']}", f"qp:{rec['qp']}"]
                        + [f"nos:{c}" for c in rec["nos_codes"][:4]]
                        + [f"course:{c}" for c in rec["courses"][:2]]
                        + [f"scheme:{s}" for s in rec["schemes"][:2]])
                paths.append(path)
                node_ids.extend(path)
        return sorted(set(node_ids)), paths

    # ---------------------------------------------------------------- fusion
    def retrieve(self, query: str, role: Role, purpose: Purpose,
                 agent_id: str = "mentor", intent: str = "qa",
                 top_k: int = 8, manifest_id: str = "",
                 kg_goal: Optional[str] = None) -> EvidenceBundle:
        vector_hits = self._vector_leg(query, role, purpose, top_k)
        bm25_hits = self._bm25_leg(query, role, purpose, agent_id, top_k)

        # Reciprocal rank fusion across the two chunk legs
        fused: dict[str, list] = {}
        for rank, (chunk, score) in enumerate(vector_hits):
            e = fused.setdefault(chunk.chunk_hash, [chunk, 0.0, 0.0])
            e[1] += 1.0 / (RRF_K + rank + 1)
            e[2] = max(e[2], score)          # keep best raw score for the gate
        for rank, (chunk, score) in enumerate(bm25_hits):
            e = fused.setdefault(chunk.chunk_hash, [chunk, 0.0, 0.0])
            e[1] += 1.0 / (RRF_K + rank + 1)
            e[2] = max(e[2], score)          # normalised BM25 evidence score

        ranked = sorted(fused.values(), key=lambda e: e[1], reverse=True)[:top_k]
        chunks = [(e[0], e[2]) for e in ranked]

        kg_nodes, kg_paths = ([], [])
        if intent == "pathway":
            kg_nodes, kg_paths = self._kg_leg(kg_goal or query)

        bundle = EvidenceBundle(chunks=chunks, kg_node_ids=kg_nodes,
                                kg_paths=kg_paths, index_manifest_id=manifest_id)
        log.info("retrieve intent=%s role=%s: %d chunks (top=%.3f), %d kg paths",
                 intent, role.value, len(chunks), bundle.top_score, len(kg_paths))
        return bundle
