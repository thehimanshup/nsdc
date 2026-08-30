"""Unit tests for Embedding Generation: get_embedder() provider wiring,
local_hash_embedder() determinism, KG-node rendering from curated CSVs
(including the scheme-row aggregation fix), and writing both unstructured
and structured embedding sets to a local (file-based, no server) Qdrant
store with correct metadata.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from backend.substrate.kg_embed import (embed_and_store_kg_nodes,
                                        load_kg_node_records)
from backend.substrate.vector_store import (get_embedder, local_hash_embedder)


# --- local_hash_embedder -----------------------------------------------

def test_local_hash_embedder_deterministic():
    embed, dim = local_hash_embedder(dim=128)
    v1 = embed(["general duty assistant"])[0]
    v2 = embed(["general duty assistant"])[0]
    assert v1 == v2
    assert len(v1) == 128


def test_local_hash_embedder_l2_normalised():
    import math
    embed, dim = local_hash_embedder(dim=64)
    v = embed(["some sample text to embed"])[0]
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


def test_local_hash_embedder_similar_text_more_similar_than_different():
    embed, dim = local_hash_embedder(dim=256)
    a, b, c = embed([
        "general duty assistant course eligibility",
        "general duty assistant course requirements",
        "mandi price of wheat in punjab today",
    ])

    def dot(x, y):
        return sum(p * q for p, q in zip(x, y))

    assert dot(a, b) > dot(a, c)


# --- get_embedder provider selection -------------------------------------

def test_get_embedder_falls_back_to_local_hash_with_no_key_no_hf(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    embed, dim, name = get_embedder()
    # In this environment neither Sarvam nor Hugging Face is reachable, so
    # the only thing that can legitimately succeed is the offline fallback.
    assert name == "local-hash-v1"
    assert dim > 0
    vecs = embed(["a test sentence"])
    assert len(vecs[0]) == dim


def test_get_embedder_explicit_local_hash_choice():
    embed, dim, name = get_embedder(provider="local_hash")
    assert name == "local-hash-v1"


def test_get_embedder_sarvam_choice_without_key_falls_back(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    embed, dim, name = get_embedder(provider="sarvam")
    # No key configured -> can't call Sarvam -> must degrade, not crash.
    assert name == "local-hash-v1"


def test_get_embedder_tries_sarvam_first_when_key_present(monkeypatch):
    # Key present but pointing nowhere real -> the HTTP call fails -> still
    # must degrade gracefully rather than raising out of get_embedder().
    monkeypatch.setenv("SARVAM_API_KEY", "fake-key-not-real")
    embed, dim, name = get_embedder()
    assert name in ("local-hash-v1", "sarvam-embed")  # never raises either way
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)


# --- KG-node rendering ----------------------------------------------------

def test_load_kg_node_records_produces_all_node_types():
    nodes = load_kg_node_records()
    types = {n.node_type for n in nodes}
    assert {"QualificationPack", "NOS", "Skill", "JobRole", "Course",
           "TrainingCentre", "Scheme", "EligibilityRule",
           "ExternalOccupation"} <= types


def test_kg_node_ids_are_unique_no_silent_collisions():
    nodes = load_kg_node_records()
    ids = [n.node_id for n in nodes]
    assert len(ids) == len(set(ids)), "duplicate node_id would silently overwrite in the store"


def test_scheme_node_aggregates_all_supported_courses():
    # Regression test for the specific bug found+fixed: nodes_scheme.csv is
    # an edge list (one row per course a scheme supports), so a naive
    # per-row render silently drops all but the last course for a scheme
    # that supports more than one.
    nodes = load_kg_node_records()
    schemes = [n for n in nodes if n.node_type == "Scheme"]
    pmkvy = next(n for n in schemes if n.node_id == "scheme:pmkvy4")
    assert "crs-gda-01" in pmkvy.text
    assert "crs-hha-01" in pmkvy.text
    assert "crs-phleb-01" in pmkvy.text
    assert pmkvy.attrs["supports_courses"] == ["crs-gda-01", "crs-hha-01", "crs-phleb-01"]


def test_kg_node_records_deterministic_across_calls():
    a = load_kg_node_records()
    b = load_kg_node_records()
    assert [n.node_id for n in a] == [n.node_id for n in b]
    assert [n.text for n in a] == [n.text for n in b]


def test_attrs_never_contains_none_key():
    # Regression test: csv.DictReader's restkey default is None for ragged
    # rows — attrs (dict[str, Any]) must never receive that.
    nodes = load_kg_node_records()
    for n in nodes:
        assert all(isinstance(k, str) for k in n.attrs.keys())


# --- writing both sets to the store with metadata -------------------------

def test_embed_and_store_kg_nodes_writes_correct_count_and_metadata(tmp_path):
    from qdrant_client import QdrantClient
    from backend.substrate.vector_store import VectorStore

    embed, dim = local_hash_embedder(dim=32)
    client = QdrantClient(path=str(tmp_path / "qdrant_local"))
    vs = VectorStore(embed, dim, collection="test_kg_nodes", client=client)
    vs.ensure_collection()

    written = embed_and_store_kg_nodes(vs, manifest_id="man-test123")
    nodes = load_kg_node_records()
    assert written == len(nodes)

    info = client.get_collection("test_kg_nodes")
    assert info.points_count == len(nodes)

    pts, _ = client.scroll("test_kg_nodes", limit=len(nodes) + 1, with_payload=True)
    qp_point = next(p for p in pts if p.payload["node_id"] == "qp:HSS/Q5101")
    assert qp_point.payload["node_type"] == "QualificationPack"
    assert qp_point.payload["index_manifest_id"] == "man-test123"
    assert "General Duty Assistant" in qp_point.payload["text"]


def test_chunks_and_kg_nodes_can_share_one_local_qdrant_client(tmp_path):
    # Regression test for the file-lock bug found+fixed: two VectorStore
    # instances pointed at the same local-mode path (without sharing a
    # client) raise "already accessed by another instance of Qdrant".
    from qdrant_client import QdrantClient
    from backend.substrate.vector_store import VectorStore

    embed, dim = local_hash_embedder(dim=16)
    path = str(tmp_path / "shared_qdrant")
    client = QdrantClient(path=path)

    chunks_vs = VectorStore(embed, dim, collection="chunks_x", client=client)
    kg_vs = VectorStore(embed, dim, collection="kg_x", client=client)
    chunks_vs.ensure_collection()
    kg_vs.ensure_collection()

    written = embed_and_store_kg_nodes(kg_vs)
    assert written > 0
    assert client.get_collection("chunks_x").points_count == 0
    assert client.get_collection("kg_x").points_count == written
