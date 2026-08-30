"""Tests for Versioning & Retrieval API: immutable version IDs
(ManifestRegistry guard), the standalone retrieval function (top-k,
filters, bm25/vector/hybrid with RRF), and source attribution on every
result."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate import retrieval_api
from backend.substrate.manifest import ChunkingConfig, IndexManifest, ManifestRegistry
from backend.substrate.schemas import (ChunkPayload, DocType, DocumentMeta,
                                       Purpose, Role, Sensitivity)


# --- immutable version IDs ---------------------------------------------

def _manifest(**over) -> IndexManifest:
    base = dict(embedding_model="bge-m3", embedding_dim=1024,
                chunking_config=ChunkingConfig())
    base.update(over)
    return IndexManifest(**base)


def test_registry_identical_resave_is_noop(tmp_path):
    reg = ManifestRegistry(tmp_path)
    m = _manifest().finalise(["h1", "h2"])
    p1 = reg.save(m)
    p2 = reg.save(_manifest().finalise(["h1", "h2"]))   # same content, same id
    assert p1 == p2
    assert reg.current_id() == m.manifest_id


def test_registry_refuses_divergent_content_under_same_id(tmp_path):
    reg = ManifestRegistry(tmp_path)
    m = _manifest().finalise(["h1"])
    reg.save(m)
    tampered = _manifest().finalise(["h1"])
    tampered.corpus_snapshot_hash = "sha256:tampered"   # mutate AFTER finalise
    with pytest.raises(ValueError, match="immutable"):
        reg.save(tampered)


def test_registry_rejects_unfinalised(tmp_path):
    with pytest.raises(ValueError, match="finalise"):
        ManifestRegistry(tmp_path).save(_manifest())


# --- retrieval fixtures ------------------------------------------------------

def _chunk(cid, text, *, doc="doc-a", section="Body", lang="en",
           roles=(Role.learner,), sens=Sensitivity.public, smode="native") -> ChunkPayload:
    return ChunkPayload(
        chunk_id=cid, doc_id=doc, section=section, text=text,
        chunk_hash=f"sha256:{cid}", language=lang, sensitivity=sens,
        allowed_roles=list(roles), allowed_purposes=[Purpose.course_guidance],
        source_org="HSSC / NQR", source_url="https://nqr.gov.in/x",
        source_license="public", source_last_updated="2026-07-01",
        source_mode=smode, index_manifest_id="man-test")


@pytest.fixture()
def corpus_file(tmp_path):
    chunks = [
        _chunk("c1", "General duty assistant course covers patient hygiene and safety."),
        _chunk("c2", "Phlebotomy technician training includes blood sample collection.",
               doc="doc-b", section="Phlebotomy"),
        _chunk("c3", "Mandi prices for wheat and rice fluctuate seasonally in Punjab.",
               doc="doc-c"),
        _chunk("c4", "Internal officer note on general duty assistant centre audits.",
               sens=Sensitivity.internal, roles=(Role.officer,)),
        _chunk("c5", "गृह स्वास्थ्य सहायक पाठ्यक्रम में रोगी देखभाल शामिल है।",
               doc="doc-d", lang="hi"),
        _chunk("c6", "General duty assistant OCR-extracted legacy eligibility text.",
               doc="doc-e", smode="ocr"),
    ]
    p = tmp_path / "chunks.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(c.model_dump_json() + "\n")
    return p


# --- bm25 leg: ranking, top-k, RBAC ---------------------------------------

def test_bm25_ranks_relevant_first_and_respects_top_k(corpus_file):
    out = retrieval_api.retrieve("general duty assistant", Role.learner,
                                 Purpose.course_guidance, top_k=2,
                                 mode="bm25", corpus_path=corpus_file)
    assert out["legs_used"] == ["bm25"]
    assert 1 <= len(out["results"]) <= 2
    assert out["results"][0]["chunk_id"] in ("c1", "c6")
    assert all("mandi" not in r["text"].lower() for r in out["results"])


def test_rbac_learner_never_sees_internal_chunk(corpus_file):
    out = retrieval_api.retrieve("general duty assistant audits", Role.learner,
                                 Purpose.course_guidance, top_k=10,
                                 mode="bm25", corpus_path=corpus_file)
    assert all(r["chunk_id"] != "c4" for r in out["results"])


def test_rbac_officer_can_see_internal_chunk(corpus_file):
    out = retrieval_api.retrieve("centre audits", Role.officer,
                                 Purpose.course_guidance, top_k=10,
                                 mode="bm25", corpus_path=corpus_file)
    assert any(r["chunk_id"] == "c4" for r in out["results"])


# --- filters -----------------------------------------------------------------

def test_language_filter(corpus_file):
    out = retrieval_api.retrieve("सहायक पाठ्यक्रम", Role.learner,
                                 Purpose.course_guidance, mode="bm25",
                                 language="hi", corpus_path=corpus_file)
    assert all(r["language"] == "hi" for r in out["results"])
    assert any(r["chunk_id"] == "c5" for r in out["results"])


def test_doc_ids_filter(corpus_file):
    out = retrieval_api.retrieve("training", Role.learner,
                                 Purpose.course_guidance, mode="bm25",
                                 doc_ids=["doc-b"], corpus_path=corpus_file)
    assert all(r["attribution"]["doc_id"] == "doc-b" for r in out["results"])


def test_source_mode_filter(corpus_file):
    out = retrieval_api.retrieve("general duty assistant", Role.learner,
                                 Purpose.course_guidance, mode="bm25",
                                 source_mode="ocr", corpus_path=corpus_file)
    assert [r["chunk_id"] for r in out["results"]] == ["c6"]


def test_section_filter(corpus_file):
    out = retrieval_api.retrieve("training blood collection", Role.learner,
                                 Purpose.course_guidance, mode="bm25",
                                 section_contains="phleb", corpus_path=corpus_file)
    assert [r["chunk_id"] for r in out["results"]] == ["c2"]


# --- attribution on every result ----------------------------------------------

def test_every_result_carries_full_attribution_and_version(corpus_file):
    out = retrieval_api.retrieve("assistant course training prices", Role.learner,
                                 Purpose.course_guidance, top_k=10,
                                 mode="bm25", corpus_path=corpus_file)
    assert out["results"], "need at least one result to assert on"
    for r in out["results"]:
        a = r["attribution"]
        assert a["source_org"] == "HSSC / NQR"
        assert a["source_url"].startswith("https://")
        assert a["source_license"] == "public"
        assert a["doc_id"] and a["section"]
        assert r["index_manifest_id"] == "man-test"
    assert out["index_manifest_id"] == "man-test"


# --- hybrid fusion --------------------------------------------------------------

class _FakeVectorStore:
    """Returns a fixed ranking so RRF behavior is deterministic."""
    def __init__(self, chunks_by_id, ranking):
        self._chunks = chunks_by_id
        self._ranking = ranking

    def search(self, query, role, purpose, top_k=8, language=None):
        return [(self._chunks[cid], 0.9 - 0.1 * i)
                for i, cid in enumerate(self._ranking) if cid in self._chunks][:top_k]


def _corpus_chunks(path):
    return {c.chunk_id: c for c in
            (ChunkPayload.model_validate_json(l) for l in path.open())}


def test_hybrid_fuses_both_legs_and_reports_them(corpus_file):
    chunks = _corpus_chunks(corpus_file)
    fake_vs = _FakeVectorStore(chunks, ranking=["c2", "c1"])
    out = retrieval_api.retrieve("general duty assistant", Role.learner,
                                 Purpose.course_guidance, top_k=5, mode="hybrid",
                                 corpus_path=corpus_file, vector_store=fake_vs)
    assert set(out["legs_used"]) == {"bm25", "vector"}
    top = out["results"][0]
    # c1 is ranked #1 by bm25 ("general duty assistant" match) and #2 by
    # vector — RRF should place it above anything ranked by only one leg.
    assert top["chunk_id"] == "c1"
    assert top["scores"]["bm25"] is not None
    assert top["scores"]["vector"] is not None


def test_hybrid_degrades_to_bm25_and_says_so(corpus_file):
    out = retrieval_api.retrieve("general duty assistant", Role.learner,
                                 Purpose.course_guidance, mode="hybrid",
                                 corpus_path=corpus_file, vector_store=None)
    assert out["legs_used"] == ["bm25"]
    assert out["results"]


def test_vector_mode_without_store_raises(corpus_file):
    with pytest.raises(RuntimeError, match="no vector index"):
        retrieval_api.retrieve("q", Role.learner, Purpose.course_guidance,
                               mode="vector", corpus_path=corpus_file)


def test_unknown_mode_rejected(corpus_file):
    with pytest.raises(ValueError, match="unknown retrieval mode"):
        retrieval_api.retrieve("q", Role.learner, Purpose.course_guidance,
                               mode="fuzzy", corpus_path=corpus_file)
