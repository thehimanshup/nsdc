"""Unit tests for the ingestion-pipeline additions: legacy/scanned-source
OCR detection, exact/near-duplicate handling + quality tagging, and
per-chunk source attribution. Sarvam Vision itself is mocked (its own
mock-fallback runs automatically with no API key, same as production).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.ingest import (_dedup_and_tag, _needs_ocr,
                                      _normalized_text_hash, _source_fields,
                                      chunk_document)
from backend.substrate.manifest import ChunkingConfig
from backend.substrate.schemas import DocType, DocumentMeta, Purpose, Role


def _meta(**over) -> DocumentMeta:
    base = dict(doc_id="doc-1", title="Test Doc", source_org="HSSC / NQR",
                source_url="https://nqr.gov.in/doc-1", doc_type=list(DocType)[0],
                license="public", last_updated="2026-01-01",
                allowed_roles=[Role.learner], allowed_purposes=[Purpose.course_guidance])
    base.update(over)
    return DocumentMeta(**base)


# --- source attribution -----------------------------------------------------

def test_source_fields_denormalized_from_meta():
    m = _meta()
    fields = _source_fields(m)
    assert fields["source_org"] == "HSSC / NQR"
    assert fields["source_url"] == "https://nqr.gov.in/doc-1"
    assert fields["source_license"] == "public"
    assert fields["source_last_updated"] == "2026-01-01"


def test_chunk_document_carries_source_attribution_on_every_chunk():
    text = "SECTION ONE\n" + ("word " * 100) + "\nSECTION TWO\n" + ("word " * 100)
    chunks = chunk_document(_meta(), text, ChunkingConfig())
    assert len(chunks) >= 2
    for c in chunks:
        assert c.source_org == "HSSC / NQR"
        assert c.source_url == "https://nqr.gov.in/doc-1"
        assert c.source_license == "public"
        assert c.source_last_updated == "2026-01-01"


def test_chunk_document_defaults_to_native_source_mode():
    chunks = chunk_document(_meta(), "SECTION\n" + ("word " * 60), ChunkingConfig())
    assert all(c.source_mode == "native" and c.ocr_confidence is None for c in chunks)


def test_chunk_document_tags_ocr_source_mode_and_confidence():
    chunks = chunk_document(_meta(), "SECTION\n" + ("word " * 60), ChunkingConfig(),
                            source_mode="ocr", ocr_confidence=0.42)
    assert all(c.source_mode == "ocr" for c in chunks)
    assert all(c.ocr_confidence == 0.42 for c in chunks)


# --- OCR routing detection ---------------------------------------------------

def test_needs_ocr_true_for_image_suffix(tmp_path):
    p = tmp_path / "scan.png"
    p.write_bytes(b"\x89PNG\r\n")
    assert _needs_ocr(p, native_text="") is True


def test_needs_ocr_false_for_text_rich_pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4")
    rich_text = "<<<PAGE 1>>>\n" + ("This is a normal text-layer PDF page. " * 20)
    assert _needs_ocr(p, native_text=rich_text) is False


def test_needs_ocr_true_for_near_empty_pdf_text(tmp_path):
    p = tmp_path / "scanned.pdf"
    p.write_bytes(b"%PDF-1.4")
    thin_text = "<<<PAGE 1>>>\n \n<<<PAGE 2>>>\n \n<<<PAGE 3>>>\nfew"
    assert _needs_ocr(p, native_text=thin_text) is True


def test_needs_ocr_false_for_non_pdf_non_image(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Heading\nSome content")
    assert _needs_ocr(p, native_text="whatever") is False


# --- dedup + quality tagging --------------------------------------------------

def _chunk(text, doc_id="doc-1", **over):
    chunks = chunk_document(_meta(doc_id=doc_id), f"SECTION\n{text}", ChunkingConfig(), **over)
    assert chunks, "test text too short to survive chunking's own 40-char floor"
    return chunks[0]


def test_dedup_drops_exact_duplicate_across_docs():
    text = "The exact same passage repeated in two different source documents here."
    c1 = _chunk(text, doc_id="doc-1")
    c2 = _chunk(text, doc_id="doc-2")
    assert c1.chunk_hash == c2.chunk_hash   # same normalised text -> same hash
    out = _dedup_and_tag([c1, c2], ChunkingConfig())
    assert len(out) == 1
    assert out[0].chunk_id == c1.chunk_id


def test_dedup_noop_when_disabled():
    text = "The exact same passage repeated in two different source documents here."
    c1 = _chunk(text, doc_id="doc-1")
    c2 = _chunk(text, doc_id="doc-2")
    out = _dedup_and_tag([c1, c2], ChunkingConfig(dedup_enabled=False))
    assert len(out) == 2


def test_near_duplicate_flagged_not_dropped():
    c1 = _chunk("Minimum qualification is class 10 pass, age above 17 years required.", doc_id="doc-1")
    c2 = _chunk("MINIMUM QUALIFICATION IS CLASS 10 PASS AGE ABOVE 17 YEARS REQUIRED",  doc_id="doc-2")
    assert c1.chunk_hash != c2.chunk_hash          # differ in case/punctuation
    assert _normalized_text_hash(c1.text) == _normalized_text_hash(c2.text)  # but normalise the same
    out = _dedup_and_tag([c1, c2], ChunkingConfig())
    assert len(out) == 2   # kept, not dropped
    flagged = [c for c in out if any(f.startswith("possible_near_duplicate_of:") for f in c.quality_flags)]
    assert len(flagged) == 1
    assert flagged[0].chunk_id == c2.chunk_id      # second occurrence is the one flagged


def test_short_chunk_quality_flag():
    # Between the hard 40-char floor and the 80-char "short" threshold.
    text = "Short passage of about fifty five characters long."
    assert 40 <= len(text) < 80
    c = _chunk(text)
    out = _dedup_and_tag([c], ChunkingConfig())
    assert "short_chunk" in out[0].quality_flags


def test_ocr_low_confidence_flagged():
    c = _chunk("A reasonably long passage that clears the short-chunk threshold easily now.",
               source_mode="ocr", ocr_confidence=0.3)
    out = _dedup_and_tag([c], ChunkingConfig())
    assert "ocr_low_confidence" in out[0].quality_flags


def test_ocr_high_confidence_not_flagged():
    c = _chunk("A reasonably long passage that clears the short-chunk threshold easily now.",
               source_mode="ocr", ocr_confidence=0.95)
    out = _dedup_and_tag([c], ChunkingConfig())
    assert "ocr_low_confidence" not in out[0].quality_flags
