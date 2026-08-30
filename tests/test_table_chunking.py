"""Unit tests for table-aware chunking (ChunkingConfig.keep_tables_intact,
previously a no-op flag — now enforced in chunk_document)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.ingest import chunk_document, _split_out_tables
from backend.substrate.manifest import ChunkingConfig
from backend.substrate.schemas import DocType, DocumentMeta, Purpose, Role


def _meta() -> DocumentMeta:
    return DocumentMeta(doc_id="tbl-doc", title="Doc With Tables", source_org="HSSC",
                        doc_type=list(DocType)[0],
                        allowed_roles=[Role.learner],
                        allowed_purposes=[Purpose.course_guidance])


_DOC = """## Eligibility Criteria
The following table lists the eligibility requirements in detail below.

| Criterion | Requirement |
|---|---|
| Education | Class 10 pass |
| Minimum age | 17 years |
| Experience | None required |

Candidates meeting all criteria may enrol at any accredited training centre.
"""


def test_split_out_tables_separates_table_from_prose():
    segments = _split_out_tables(_DOC)
    kinds = [k for k, _ in segments]
    assert "table" in kinds and "text" in kinds
    table_seg = next(s for k, s in segments if k == "table")
    assert "| Education | Class 10 pass |" in table_seg


def test_table_emitted_as_single_atomic_chunk():
    chunks = chunk_document(_meta(), _DOC, ChunkingConfig())
    table_chunks = [c for c in chunks if "#s1-t" in c.chunk_id]
    assert len(table_chunks) == 1
    t = table_chunks[0]
    # entire table in one chunk, all rows present
    assert "| Education | Class 10 pass |" in t.text
    assert "| Experience | None required |" in t.text
    # row structure (newlines) preserved — tables are NOT whitespace-flattened
    assert "\n" in t.text


def test_table_never_split_even_when_over_token_budget():
    rows = "\n".join(f"| item-{i} | value {i} with several words here |" for i in range(120))
    doc = f"## Big Table\nIntro paragraph explaining the very large table below in detail.\n\n| Col A | Col B |\n|---|---|\n{rows}\n"
    cfg = ChunkingConfig(target_tokens=50)   # far smaller than the table
    chunks = chunk_document(_meta(), doc, cfg)
    table_chunks = [c for c in chunks if "-t" in c.chunk_id.split("#")[1]]
    assert len(table_chunks) == 1, "an over-budget table must remain one atomic chunk"
    assert "item-0" in table_chunks[0].text and "item-119" in table_chunks[0].text
    assert "oversized_table_chunk" in table_chunks[0].quality_flags


def test_prose_around_table_still_chunked_normally():
    chunks = chunk_document(_meta(), _DOC, ChunkingConfig())
    text_chunks = [c for c in chunks if "#s1-c" in c.chunk_id]
    assert any("eligibility requirements" in c.text for c in text_chunks)
    assert any("accredited training centre" in c.text for c in text_chunks)


def test_flag_off_reverts_to_plain_chunking():
    chunks = chunk_document(_meta(), _DOC, ChunkingConfig(keep_tables_intact=False))
    assert not any("-t" in c.chunk_id.split("#")[1] for c in chunks)


def test_document_without_tables_unaffected():
    doc = "## Plain Section\n" + ("This is ordinary prose content for the section. " * 10)
    with_flag = chunk_document(_meta(), doc, ChunkingConfig(keep_tables_intact=True))
    without_flag = chunk_document(_meta(), doc, ChunkingConfig(keep_tables_intact=False))
    assert [c.text for c in with_flag] == [c.text for c in without_flag]
