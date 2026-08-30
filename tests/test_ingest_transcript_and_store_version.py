"""Unit tests for the video-transcript chunking path (backend.substrate.ingest)
and the vector_store_backend / store_schema_version manifest fields
(backend.substrate.manifest). No running server needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.ingest import (chunk_transcript, parse_transcript_cues,
                                      _strip_transcript_markup)
from backend.substrate.manifest import ChunkingConfig, IndexManifest
from backend.substrate.schemas import DocType, DocumentMeta, Purpose, Role

_SRT = """1
00:00:00,000 --> 00:00:03,500
Namaste, welcome to this course on general duty assistance.

2
00:00:03,500 --> 00:00:07,000
Today we will cover patient hygiene and safety basics.

3
00:00:07,000 --> 00:00:10,200
Let us begin with hand-washing technique.
"""

_VTT = """WEBVTT

00:00.000 --> 00:03.500
Namaste, welcome to this course.

00:03.500 --> 00:07.000
Today we cover patient hygiene.
"""


def _meta(doc_id="course-vid-01") -> DocumentMeta:
    return DocumentMeta(doc_id=doc_id, title="GDA intro video", source_org="NSDC",
                        doc_type=list(DocType)[0],
                        allowed_roles=[Role.learner],
                        allowed_purposes=[Purpose.course_guidance])


# --- cue parsing -----------------------------------------------------------

def test_parse_srt_cues():
    cues = parse_transcript_cues(_SRT)
    assert len(cues) == 3
    assert cues[0].start == 0.0 and cues[0].end == 3.5
    assert cues[-1].end == 10.2


def test_parse_vtt_cues_no_hours_field():
    cues = parse_transcript_cues(_VTT)
    assert len(cues) == 2
    assert cues[0].start == 0.0 and cues[0].end == 3.5


def test_strip_transcript_markup_drops_cue_numbers_and_timestamps():
    text = _strip_transcript_markup(_SRT)
    assert "-->" not in text
    assert "00:00:00" not in text
    assert "Namaste" in text
    assert "hand-washing" in text


# --- chunking ---------------------------------------------------------------

def test_chunk_transcript_merges_into_single_segment_for_large_target():
    cfg = ChunkingConfig(target_tokens=200, overlap_tokens=0)
    chunks = chunk_transcript(_meta(), _SRT, cfg)
    assert len(chunks) == 1
    assert chunks[0].start_ts == 0.0
    assert chunks[0].end_ts == 10.2
    assert "Namaste" in chunks[0].text and "hand-washing" in chunks[0].text


def test_chunk_transcript_splits_on_small_target_with_correct_boundaries():
    cfg = ChunkingConfig(target_tokens=6, overlap_tokens=0)
    chunks = chunk_transcript(_meta(), _SRT, cfg)
    assert len(chunks) == 3
    # boundaries must land on whole cues, never mid-cue
    assert (chunks[0].start_ts, chunks[0].end_ts) == (0.0, 3.5)
    assert (chunks[1].start_ts, chunks[1].end_ts) == (3.5, 7.0)
    assert (chunks[2].start_ts, chunks[2].end_ts) == (7.0, 10.2)
    # ids are stable/sequential and section is a synthetic segment label
    assert chunks[0].chunk_id == "course-vid-01#seg1"
    assert chunks[0].section.startswith("Segment")


def test_chunk_transcript_chunks_carry_governance_fields_from_meta():
    cfg = ChunkingConfig(target_tokens=200)
    chunks = chunk_transcript(_meta(), _SRT, cfg)
    c = chunks[0]
    assert c.allowed_roles == [Role.learner]
    assert c.allowed_purposes == [Purpose.course_guidance]
    assert c.chunk_hash.startswith("sha256:")


def test_page_field_unused_for_transcript_chunks():
    # Transcript chunks use start_ts/end_ts, not page — confirm they don't
    # collide or get accidentally populated.
    chunks = chunk_transcript(_meta(), _SRT, ChunkingConfig(target_tokens=200))
    assert chunks[0].page is None


# --- manifest: store version now part of the deterministic hash ------------

def _base_manifest(**overrides) -> IndexManifest:
    base = dict(embedding_model="bge-m3", embedding_dim=1024,
                chunking_config=ChunkingConfig())
    base.update(overrides)
    return IndexManifest(**base)


def test_manifest_defaults_to_qdrant_backend():
    m = _base_manifest().finalise(["h1"])
    assert m.vector_store_backend == "qdrant"
    assert m.store_schema_version == "1"


def test_manifest_id_changes_when_store_backend_changes():
    m_qdrant = _base_manifest(vector_store_backend="qdrant").finalise(["h1", "h2"])
    m_pgvector = _base_manifest(vector_store_backend="pgvector").finalise(["h1", "h2"])
    assert m_qdrant.manifest_id != m_pgvector.manifest_id


def test_manifest_id_changes_when_store_schema_version_changes():
    m_v1 = _base_manifest(store_schema_version="1").finalise(["h1"])
    m_v2 = _base_manifest(store_schema_version="2").finalise(["h1"])
    assert m_v1.manifest_id != m_v2.manifest_id


def test_manifest_id_stable_for_identical_inputs():
    # The core deterministic-rebuild property: same inputs -> same id.
    m1 = _base_manifest().finalise(["h1", "h2"])
    m2 = _base_manifest().finalise(["h2", "h1"])  # order-independent
    assert m1.manifest_id == m2.manifest_id
