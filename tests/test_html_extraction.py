"""Tests for structure-preserving HTML extraction (_extract_html) — the
bs4 upgrade over naive tag stripping."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.ingest import _extract_html, chunk_document
from backend.substrate.manifest import ChunkingConfig
from backend.substrate.schemas import DocType, DocumentMeta, Purpose, Role

_HTML = """<html><head><style>p{color:red}</style><script>evil()</script></head><body>
<nav>Home | About</nav>
<h2>Eligibility Criteria</h2>
<p>The following requirements apply to all candidates seeking enrolment.</p>
<table><tr><th>Criterion</th><th>Requirement</th></tr>
<tr><td>Education</td><td>Class 10 pass</td></tr>
<tr><td>Minimum age</td><td>17 years</td></tr></table>
<h2>Assessment</h2><p>Assessment is conducted by HSSC certified assessors at the centre.</p>
<footer>Copyright 2026</footer></body></html>"""


def _meta():
    return DocumentMeta(doc_id="html-doc", title="T", source_org="X",
                        doc_type=list(DocType)[0],
                        allowed_roles=[Role.learner],
                        allowed_purposes=[Purpose.course_guidance])


def test_headings_become_markdown_sections():
    text = _extract_html(_HTML)
    assert "## Eligibility Criteria" in text
    assert "## Assessment" in text


def test_tables_become_markdown_tables():
    text = _extract_html(_HTML)
    assert "| Education | Class 10 pass |" in text
    assert "|---|" in text


def test_boilerplate_dropped():
    text = _extract_html(_HTML)
    assert "evil()" not in text
    assert "color:red" not in text
    assert "Copyright 2026" not in text
    assert "Home | About" not in text


def test_html_sections_flow_into_chunker():
    chunks = chunk_document(_meta(), _extract_html(_HTML), ChunkingConfig())
    sections = {c.section for c in chunks}
    assert "Eligibility Criteria" in sections and "Assessment" in sections


def test_html_table_becomes_atomic_table_chunk():
    chunks = chunk_document(_meta(), _extract_html(_HTML), ChunkingConfig())
    table_chunks = [c for c in chunks if "-t" in c.chunk_id.split("#")[1]]
    assert len(table_chunks) == 1
    assert "| Minimum age | 17 years |" in table_chunks[0].text


def test_plain_text_document_fallback():
    assert "just words" in _extract_html("<html><body>just words</body></html>")
