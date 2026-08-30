"""Substrate query service — the governed Graph-RAG pipeline (ST-404).

    query → intent → hybrid retrieve (RBAC pre-filter) → evidence gate
          → composer (citation contract) → citation hard gate (retry→block)
          → audit event → response

Wired into FastAPI by backend/routes_substrate.py behind SUBSTRATE_RAG=true.
Runs degraded-gracefully: without Qdrant/Neo4j only the BM25 leg is active
(mock/offline demo mode); with services up, all three legs fuse.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .composer import compose
from .gates import (EvidenceBundle, blocked_output_contract, enforce_citations,
                    evidence_gate, no_evidence_contract, safety_gate,
                    unsafe_request_contract)
from .manifest import ManifestRegistry
from .retriever import HybridRetriever, _rbac_ok
from .schemas import (ChunkPayload, CitationContract, Purpose, RefusalReason,
                      Role)

log = logging.getLogger("substrate.service")

AGENT_CORPUS_ID = "skilling_core"

_PATHWAY_HINTS = re.compile(
    r"\b(career|pathway|path|job|naukri|kaam|course.*(after|next)|which course|"
    r"कौन सा कोर्स|नौकरी|करियर)\b", re.I)

_HI_CHARS = re.compile(r"[ऀ-ॿ]")

_GREETING = re.compile(
    r"^\s*(hi+|hii+|hello+|hey+|namaste|नमस्ते|नमस्कार|hola|yo|"
    r"good\s*(morning|afternoon|evening|day)|"
    r"thanks?|thank\s*you|धन्यवाद|शुक्रिया|ok(ay)?|bye|goodbye)[\s!.,🙏😊]*$", re.I)

_DEFAULT_OPENERS = {
    "en": "Namaste! I'm your Skill Mentor — tell me your education level and "
          "the kind of work you're interested in, and I'll find matching "
          "courses and schemes with sources.",
    "hi": "नमस्ते! मैं आपका स्किल मेंटर हूँ — अपनी शिक्षा और रुचि का क्षेत्र बताइए, "
          "मैं प्रमाणों के साथ उपयुक्त कोर्स और योजनाएँ खोजूँगा।",
}


def detect_language(text: str) -> str:
    return "hi" if _HI_CHARS.search(text) else "en"


def detect_intent(text: str) -> str:
    return "pathway" if _PATHWAY_HINTS.search(text) else "qa"


@dataclass
class QueryResult:
    contract: CitationContract
    interaction_id: str
    latency_ms: int
    retrieval: dict = field(default_factory=dict)
    compose_mode: str = ""
    gates: dict = field(default_factory=dict)


class SubstrateService:
    def __init__(self, data_dir: str = "data"):
        self.registry = ManifestRegistry(data_dir)
        self.manifest_id = self.registry.current_id() or ""
        self.retriever = HybridRetriever(
            vector_store=self._try_vector(),
            bm25_retrieve=self._bm25_adapter(),
            kg_session_factory=self._try_kg())

    # ---------------------------------------------------------------- legs
    def _try_vector(self):
        url = os.getenv("QDRANT_URL", "")
        if not url:
            log.info("QDRANT_URL unset — vector leg disabled (BM25-only mode)")
            return None
        try:
            from .vector_store import VectorStore, bge_m3_embedder
            embed, dim = bge_m3_embedder()
            vs = VectorStore(embed, dim, url=url)
            vs.ensure_collection()
            return vs
        except Exception as e:
            log.warning("vector leg unavailable (%s) — continuing without", e)
            return None

    def _try_kg(self):
        uri = os.getenv("NEO4J_URI", "")
        if not uri:
            log.info("NEO4J_URI unset — KG leg disabled")
            return None
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                uri, auth=(os.getenv("NEO4J_USER", "neo4j"),
                           os.getenv("NEO4J_PASSWORD", "substrate-dev-pass")))
            driver.verify_connectivity()
            return driver.session
        except Exception as e:
            log.warning("KG leg unavailable (%s) — continuing without", e)
            return None

    def _bm25_adapter(self):
        """Adapt phase6e retrieval to yield governance-carrying dicts.

        Two-pass retrieval: a FOCUS pass on informative/code terms first
        (so 'healthcare' beats 'course' for topical relevance), then the
        full query. Scores are normalised per pass and merged by max, so
        the focused hits keep priority."""
        from ..retrieval.pipeline import retrieve_with_meta
        from .retriever import focus_query

        def bm25(agent_id: str, query: str, k: int = 8):
            merged: dict[str, tuple] = {}
            passes = []
            fq = focus_query(query)
            if fq and fq.lower() != query.lower().strip():
                passes.append((fq, 1.0))       # focus pass, full weight
            passes.append((query, 0.9))        # original query, slight discount
            for q, weight in passes:
                hits = retrieve_with_meta(AGENT_CORPUS_ID, q, k=k)
                top = max((s for _, s in hits), default=0.0)
                for chunk, score in hits:
                    rel = (score / top if top > 0 else 0.0) * weight
                    prev = merged.get(chunk.chunk_id)
                    if prev is None or rel > prev[1]:
                        merged[chunk.chunk_id] = (chunk, rel)
            out = []
            for chunk, rel in sorted(merged.values(), key=lambda e: -e[1]):
                md = chunk.metadata or {}
                if not md.get("allowed_roles"):
                    continue  # legacy chunk without governance — never serve
                out.append(({
                    "chunk_id": chunk.chunk_id, "doc_id": md.get("doc_id", ""),
                    "section": md.get("section", ""), "page": md.get("page"),
                    "text": chunk.body, "chunk_hash": md.get("chunk_hash", ""),
                    "language": "hi" if chunk.language.startswith("hi") else "en",
                    "sensitivity": md.get("sensitivity", "restricted"),
                    "allowed_roles": md.get("allowed_roles", []),
                    "allowed_purposes": md.get("allowed_purposes", []),
                    "kg_node_ids": md.get("kg_node_ids", []),
                    "index_manifest_id": self.manifest_id,
                }, float(rel)))
                if len(out) >= k:
                    break
            return out
        return bm25

    # ---------------------------------------------------------------- query
    async def query(self, question: str, role: Role, purpose: Purpose,
                    agent_id: str = "mentor",
                    language: Optional[str] = None,
                    actor: str = "anonymous") -> QueryResult:
        t0 = time.perf_counter()
        interaction_id = "int_" + uuid.uuid4().hex[:12]
        language = language or detect_language(question)
        intent = detect_intent(question)

        # Small talk / greetings — friendly reply, no retrieval, no gates.
        if _GREETING.match(question):
            opener = self._agent_opener(agent_id, language)
            contract = CitationContract(answer_markdown=opener, claims=[],
                                        confidence=1.0, language=language,
                                        index_manifest_id=self.manifest_id)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            info = {"chunks": [], "kg_paths": [], "intent": "greeting",
                    "manifest_id": self.manifest_id}
            self._audit(actor, role, purpose, agent_id, interaction_id,
                        question, info, contract, {"greeting": {"allowed": True}},
                        "greeting", latency_ms)
            return QueryResult(contract=contract, interaction_id=interaction_id,
                               latency_ms=latency_ms, retrieval=info,
                               compose_mode="greeting",
                               gates={"greeting": {"allowed": True, "detail": ""}})

        sg = safety_gate(question)
        if not sg.allowed:
            contract = unsafe_request_contract(language, self.manifest_id,
                                               reason=sg.refusal_reason,
                                               detail=sg.detail)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            info = {"chunks": [], "kg_paths": [], "intent": intent,
                    "manifest_id": self.manifest_id}
            self._audit(actor, role, purpose, agent_id, interaction_id,
                        question, info, contract,
                        {"safety": {"allowed": False, "detail": sg.detail}},
                        "n/a", latency_ms)
            return QueryResult(contract=contract, interaction_id=interaction_id,
                               latency_ms=latency_ms, retrieval=info,
                               compose_mode="n/a",
                               gates={"safety": {"allowed": False,
                                                 "detail": sg.detail}})

        bundle = self.retriever.retrieve(
            question, role, purpose, agent_id=agent_id, intent=intent,
            manifest_id=self.manifest_id)

        gates: dict = {}
        from .gates import superlative_gate
        sup = superlative_gate(question, bundle)
        if not sup.allowed:
            gates["superlative"] = {"allowed": False, "detail": sup.detail}
        ev = evidence_gate(bundle) if sup.allowed else sup
        gates["evidence"] = {"allowed": ev.allowed, "detail": ev.detail}
        compose_mode = "n/a"

        if not ev.allowed:
            contract = no_evidence_contract(language, manifest_id=self.manifest_id)
        else:
            agent_system = self._agent_system(agent_id)
            from ..llm import get_llm_for
            llm = get_llm_for(self._agent_provider(agent_id))
            contract, compose_mode = await compose(
                llm, question, bundle, agent_system, language)
            cg = enforce_citations(contract)
            gates["citation"] = {"allowed": cg.allowed, "detail": cg.detail}
            if not cg.allowed:
                # one regeneration attempt, then block (FR-9 hard gate)
                contract, compose_mode = await compose(
                    llm, question, bundle, agent_system, language, max_attempts=1)
                cg2 = enforce_citations(contract)
                gates["citation_retry"] = {"allowed": cg2.allowed, "detail": cg2.detail}
                if not cg2.allowed:
                    contract = blocked_output_contract(language, self.manifest_id)
                    compose_mode += "+blocked"

        latency_ms = int((time.perf_counter() - t0) * 1000)
        retrieval_info = {
            "chunks": [(c.chunk_id, round(s, 3)) for c, s in bundle.chunks],
            "kg_paths": bundle.kg_paths, "intent": intent,
            "manifest_id": self.manifest_id,
        }
        self._audit(actor, role, purpose, agent_id, interaction_id, question,
                    retrieval_info, contract, gates, compose_mode, latency_ms)

        # ST-504: async groundedness judge — never blocks the response path.
        if not contract.is_refusal and contract.claims:
            from .judge import fire_and_forget
            chunks_by_id = {c.chunk_id: c.text for c, _ in bundle.chunks}
            fire_and_forget(interaction_id, contract, chunks_by_id,
                            os.getenv("DATA_DIR", "data"))
        return QueryResult(contract=contract, interaction_id=interaction_id,
                           latency_ms=latency_ms, retrieval=retrieval_info,
                           compose_mode=compose_mode, gates=gates)

    # -------------------------------------------------------------- helpers
    def _agent_opener(self, agent_id: str, language: str) -> str:
        if language == "en":
            try:
                from ..store import store
                opener = getattr(store.get_agent(agent_id), "signature_opener", "")
                if opener:
                    return opener
            except Exception:
                pass
        return _DEFAULT_OPENERS.get(language, _DEFAULT_OPENERS["en"])

    def _agent_system(self, agent_id: str) -> str:
        try:
            from ..store import store
            agent = store.get_agent(agent_id)
            return getattr(agent, "department_block", "") or ""
        except Exception:
            return ("Ground every factual claim in the provided evidence with "
                    "citations. Refuse when evidence is missing.")

    def _agent_provider(self, agent_id: str) -> Optional[str]:
        try:
            from ..store import store
            return getattr(store.get_agent(agent_id), "llm_provider", None)
        except Exception:
            return None

    def _audit(self, actor, role, purpose, agent_id, interaction_id, question,
               retrieval_info, contract: CitationContract, gates, mode, latency_ms):
        try:
            from ..audit import append_event
            append_event(
                actor=actor,
                action="substrate.query",
                resource={"agentId": agent_id, "interactionId": interaction_id},
                payload={
                    "role": role.value, "purpose": purpose.value,
                    "prompt": question[:500],
                    "retrieved_chunk_ids": [c for c, _ in retrieval_info["chunks"]],
                    "kg_paths": retrieval_info["kg_paths"],
                    "index_manifest_id": retrieval_info["manifest_id"],
                    "refusal_reason": contract.refusal_reason.value
                        if contract.refusal_reason else None,
                    "claims": len(contract.claims),
                    "uncited_claims": len(contract.uncited_claims()),
                    "compose_mode": mode, "gates": gates,
                    "latency_ms": latency_ms,
                })
        except Exception as e:  # audit must never take the answer down…
            log.error("AUDIT WRITE FAILED for %s: %s", interaction_id, e)
            # …but an unauditable substrate is a defect — surface loudly.


_service: Optional[SubstrateService] = None


def get_service() -> SubstrateService:
    global _service
    if _service is None:
        _service = SubstrateService(os.getenv("DATA_DIR", "data"))
    return _service
