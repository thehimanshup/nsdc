"""Register-driven corpus ingestion (ST-203, PRD FR-1/2).

Reads corpus/SOURCE_REGISTER.csv (the governance source of truth),
extracts text from corpus/raw/<doc_id>.<ext>, chunks section-aware,
and produces:

  1. corpus/curated/chunks.jsonl        — governed ChunkPayload records
  2. data/manifests/man-*.json          — deterministic index manifest
  3. data/corpora/skilling_core/chunks.jsonl — phase6e BM25 store export
     (so the reused keyword leg works immediately, before Qdrant is up)
  4. optional Qdrant upsert (--qdrant) when the vector stack is running

FAIL CLOSED: a register row missing mandatory metadata aborts ingestion
of that document; a raw file with no register row is never ingested.

Legacy/scanned sources (image files, or PDFs with a near-empty text
layer) are routed through Sarvam Vision OCR (backend.vision) instead of
pdfplumber's native text extraction — see _needs_ocr()/_ocr_extract_text().
Runs in Sarvam's existing mock-fallback mode when no API key is
configured, same as the rest of the app.

Exact-duplicate chunks (identical normalised text, anywhere in the
corpus) are dropped at ingest time; near-duplicates are kept but flagged
in quality_flags — see _dedup_and_tag(). Controlled by
ChunkingConfig.ocr_enabled / ChunkingConfig.dedup_enabled, both of which
are part of the deterministic manifest hash (manifest.py).

Usage:
    python -m backend.substrate.ingest            # BM25 export only
    python -m backend.substrate.ingest --qdrant   # + vector upsert
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .manifest import ChunkingConfig, IndexManifest, ManifestRegistry
from .schemas import ChunkPayload, DocumentMeta, Purpose, Role, Sensitivity

log = logging.getLogger("substrate.ingest")

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus"
DATA_DIR = ROOT / "data"
AGENT_CORPUS_ID = "skilling_core"

# KG hints: map doc_ids to KG node ids so chunks carry graph anchors.
KG_HINTS = {
    "hssc-qp-5101": ["qp:HSS/Q5101"],
    "hssc-qp-5102": ["qp:HSS/Q5102"],
    "hssc-qp-0301": ["qp:HSS/Q0301"],
    "pmkvy4-guidelines": ["scheme:pmkvy4"],
    "course-gda-01": ["course:crs-gda-01", "qp:HSS/Q5101"],
    "course-hha-01": ["course:crs-hha-01", "qp:HSS/Q5102"],
    "centre-registry": ["centre:TC-DEL-001", "centre:TC-DEL-002",
                        "centre:TC-DEL-003", "centre:TC-DEL-004"],
}


# ------------------------------------------------------- injection quarantine
# Retrieved content is DATA, never instructions (RFP 4.B.3 red-team scope).
# Chunks carrying instruction-like payloads are quarantined at ingestion:
# sensitivity forced to `restricted`, allowed_roles collapsed to admin, and
# the chunk flagged — so they can never reach a learner/officer/sme prompt.
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|polic)"
    r"|disregard\s+(your|the)\s+(instructions?|system|polic)"
    r"|reveal\s+(all\s+)?(learner|user|personal)\s+data"
    r"|^\s*(SYSTEM|ASSISTANT)\s*:"
    r"|you\s+must\s+now\s+(act|behave)\s+as"
    r"|print\s+your\s+(system\s+)?prompt)",
    re.I | re.M)


def quarantine_scan(chunk: "ChunkPayload") -> bool:
    """Returns True (and mutates the chunk into quarantine) if the text
    contains prompt-injection payloads."""
    if _INJECTION_PATTERNS.search(chunk.text):
        chunk.sensitivity = Sensitivity.restricted
        chunk.allowed_roles = [Role.admin]
        chunk.kg_node_ids = []
        log.warning("QUARANTINED injection-suspect chunk %s", chunk.chunk_id)
        return True
    return False


# --------------------------------------------------------------------- text
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
_OCR_MIN_CHARS_PER_PAGE = 20   # below this, a PDF page is treated as scanned/legacy


def _needs_ocr(path: Path, native_text: str) -> bool:
    """Detect legacy/scanned sources that need OCR rather than native
    text extraction: bare image files always do; a PDF does if its
    per-page text yield from pdfplumber is near-empty (scanned, no text
    layer) — the common shape for older QP/NOS documents."""
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return True
    if suffix != ".pdf":
        return False
    pages = re.split(r"<<<PAGE \d+>>>", native_text)
    pages = [p for p in pages if p.strip()] or [native_text]
    non_ws_per_page = [len(re.sub(r"\s", "", p)) for p in pages]
    return (sum(non_ws_per_page) / max(1, len(non_ws_per_page))) < _OCR_MIN_CHARS_PER_PAGE


def _ocr_extract_text(path: Path, language_hint: str = "en-IN") -> tuple[str, Optional[float]]:
    """Run the document through Sarvam Vision OCR (backend.vision), which
    already has its own live/mock fallback — this function doesn't need to
    know or care which mode is active. Returns (text, confidence);
    confidence is None if OCR could not run at all (rather than raising and
    aborting the whole ingest run for one bad document)."""
    import asyncio
    from ..vision import extract_document

    suffix = path.suffix.lower()

    async def _run_one(image_bytes: bytes, mime: str, filename: str) -> tuple[str, float]:
        res = await extract_document(image_bytes=image_bytes, mime_type=mime,
                                     filename=filename, hint_type="auto",
                                     language_hint=language_hint)
        return (res.raw_text or ""), res.confidence

    if suffix in _IMAGE_SUFFIXES:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "tif": "image/tiff", "tiff": "image/tiff", "bmp": "image/bmp"
                }[suffix.lstrip(".")]
        try:
            text, conf = asyncio.run(_run_one(path.read_bytes(), mime, path.name))
            return text, conf
        except Exception as e:
            log.error("OCR failed for image %s: %s", path.name, e)
            return "", None

    if suffix == ".pdf":
        try:
            import io
            import pdfplumber
        except ImportError as e:
            raise RuntimeError("pip install pdfplumber for PDF OCR rasterization") from e
        texts, confs = [], []
        try:
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    img = page.to_image(resolution=200).original  # PIL.Image
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    text, conf = asyncio.run(
                        _run_one(buf.getvalue(), "image/png", f"{path.stem}-p{i}.png"))
                    texts.append(f"\n<<<PAGE {i}>>>\n" + text)
                    confs.append(conf)
        except Exception as e:
            log.error("OCR rasterization failed for %s: %s — falling back to native text", path.name, e)
            return "", None
        avg_conf = sum(confs) / len(confs) if confs else None
        return "\n".join(texts), avg_conf

    return "", None


def _extract_html(raw: str) -> str:
    """Structure-preserving HTML extraction (upgrade over naive tag
    stripping — see docs/design/design_and_setup_decisions.md §2 follow-up):

      - <h1>-<h4> become markdown headings, so _sections() splits on the
        document's real structure instead of hoping ALL-CAPS lines exist
      - <table> becomes a markdown table, so the table-aware chunker
        (keep_tables_intact) emits it as one atomic chunk with rows intact
      - <script>/<style>/<nav>/<footer> boilerplate is dropped entirely
      - block elements get line breaks (naive stripping ran paragraphs
        together into one soup of words)

    Falls back to the previous regex stripping if beautifulsoup4 isn't
    installed, so HTML ingestion degrades rather than breaks.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed — falling back to naive "
                    "HTML tag stripping (headings/tables lose structure)")
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        return re.sub(r"<[^>]+>", " ", stripped)

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()

    out: list[str] = []

    def _table_to_markdown(table) -> str:
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        if not rows:
            return ""
        # header separator after the first row so it parses as a md table
        sep = "|" + "---|" * (rows[0].count("|") - 1)
        return "\n".join([rows[0], sep] + rows[1:])

    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "pre"],
                            recursive=True):
        if el.find_parent("table") is not None and el.name != "table":
            continue          # cell contents are handled by their table
        if el.name == "table":
            md = _table_to_markdown(el)
            if md:
                out.append(md)
        elif el.name in ("h1", "h2", "h3", "h4"):
            level = int(el.name[1])
            heading = el.get_text(" ", strip=True)
            if heading:
                out.append("#" * level + " " + heading)
        else:
            text = el.get_text(" ", strip=True)
            if text:
                out.append(text)

    if out:
        return "\n\n".join(out)
    # document with no recognized block elements — plain text fallback
    return soup.get_text(" ", strip=True)


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".srt", ".vtt"):
        # Handled by a dedicated timestamp-aware path — chunk_document()
        # dispatches on suffix too, this branch only covers _extract_text()
        # callers that just want raw text (e.g. a future full-text search
        # export), so strip cue numbers/timestamps and keep spoken text only.
        return _strip_transcript_markup(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in (".html", ".htm"):
        return _extract_html(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".pdf":
        try:
            import pdfplumber  # lazy optional dep
        except ImportError as e:
            raise RuntimeError("pip install pdfplumber for PDF ingestion") from e
        out = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                out.append(f"\n<<<PAGE {i}>>>\n" + (page.extract_text() or ""))
        return "\n".join(out)
    raise RuntimeError(f"unsupported file type: {path.name}")


def _sections(text: str) -> list[tuple[str, str]]:
    """Split on markdown headings / ALL-CAPS headings; fallback single section."""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title, buf = "Document", []
    heading = re.compile(r"^(#{1,4}\s+.+|[A-Z][A-Z0-9 /&\-]{6,60}:?\s*)$")
    for ln in lines:
        if heading.match(ln.strip()) and len(ln.strip()) < 80:
            if buf and any(s.strip() for s in buf):
                sections.append((current_title, buf))
            current_title, buf = ln.strip().lstrip("#").strip().rstrip(":"), []
        else:
            buf.append(ln)
    if buf and any(s.strip() for s in buf):
        sections.append((current_title, buf))
    return [(t, "\n".join(b).strip()) for t, b in sections if "\n".join(b).strip()]


# ------------------------------------------------------- video-transcript
# SRT:  "00:01:23,456 --> 00:01:26,000"
# VTT:  "00:01:23.456 --> 00:01:26.000"  (also allows "01:23.456" — no hours)
_CUE_TIME = r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
_CUE_ARROW = re.compile(rf"{_CUE_TIME}\s*-->\s*{_CUE_TIME}")


def _cue_seconds(h: Optional[str], m: str, s: str, ms: str) -> float:
    h = int(h) if h else 0
    return h * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _strip_transcript_markup(raw: str) -> str:
    """Plain spoken-text extraction (no timestamps) — used when a caller
    just wants full text, not timestamp-aware chunks."""
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln == "WEBVTT" or ln.isdigit() or _CUE_ARROW.search(ln):
            continue
        lines.append(ln)
    return "\n".join(lines)


@dataclass
class TranscriptCue:
    start: float
    end: float
    text: str


def parse_transcript_cues(raw: str) -> list["TranscriptCue"]:
    """Parse SRT or WebVTT cues. Tolerant of both formats since they only
    differ in decimal separator (',' vs '.') and hour-optionality — one
    parser covers both rather than branching on file extension twice."""
    cues: list[TranscriptCue] = []
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        text_lines = []
        start = end = None
        for ln in lines:
            m = _CUE_ARROW.search(ln)
            if m:
                start = _cue_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
                end = _cue_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
                continue
            if ln.strip().isdigit() or ln.strip() == "WEBVTT":
                continue
            text_lines.append(ln.strip())
        if start is not None and text_lines:
            cues.append(TranscriptCue(start=start, end=end, text=" ".join(text_lines)))
    return cues


def _source_fields(meta: DocumentMeta) -> dict:
    """Attribution fields denormalized onto every chunk at ingest time —
    see docs/design/design_and_setup_decisions.md §4/§5. Chunk carries its
    own provenance even if the register changes or the chunk is inspected
    standalone (e.g. an audit export)."""
    return dict(
        source_org=meta.source_org,
        source_url=meta.source_url,
        source_license=meta.license,
        source_last_updated=meta.last_updated,
    )


def chunk_transcript(meta: DocumentMeta, raw: str, cfg: ChunkingConfig) -> list[ChunkPayload]:
    """Timestamp-aware chunking for video/audio transcripts (SRT/VTT).

    Merges consecutive cues into ~target_tokens windows, same budget as text
    chunking (one shared ChunkingConfig, not a second parallel config to keep
    in sync) — but a chunk boundary always falls on a whole cue, never mid-
    cue, so start_ts/end_ts point at an exact, playable moment rather than
    an approximate offset.
    """
    cues = parse_transcript_cues(raw)
    target_words = int(cfg.target_tokens / 0.75)
    chunks: list[ChunkPayload] = []
    seg_no = 0
    buf: list[TranscriptCue] = []
    buf_words = 0
    source_fields = _source_fields(meta)

    def _flush():
        nonlocal seg_no, buf, buf_words
        if not buf:
            return
        seg_no += 1
        text = re.sub(r"\s+", " ", " ".join(c.text for c in buf)).strip()
        if len(text) >= 40:
            chunks.append(ChunkPayload(
                chunk_id=f"{meta.doc_id}#seg{seg_no}",
                doc_id=meta.doc_id, section=f"Segment {seg_no}",
                start_ts=buf[0].start, end_ts=buf[-1].end, text=text,
                chunk_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                language=meta.language, sensitivity=meta.sensitivity,
                allowed_roles=meta.allowed_roles,
                allowed_purposes=meta.allowed_purposes,
                kg_node_ids=KG_HINTS.get(meta.doc_id, []),
                **source_fields))
        buf, buf_words = [], 0

    for cue in cues:
        n = len(cue.text.split())
        if buf_words + n > target_words and buf:
            _flush()
        buf.append(cue)
        buf_words += n
    _flush()
    return chunks


def _split_words(text: str, target: int, overlap: int) -> list[str]:
    words = text.split()
    if len(words) <= target:
        return [text]
    out, i = [], 0
    while i < len(words):
        out.append(" ".join(words[i:i + target]))
        i += max(1, target - overlap)
    return out


_TABLE_ROW = re.compile(r"^\s*\|.+\|\s*$")


def _split_out_tables(body: str) -> list[tuple[str, str]]:
    """Split a section body into ('text', ...) and ('table', ...) segments.
    A table is a run of consecutive markdown-table rows (|...|...|). Order
    is preserved so surrounding prose stays adjacent to its table."""
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    mode = "text"
    for ln in body.splitlines():
        this = "table" if _TABLE_ROW.match(ln) else "text"
        if this != mode and buf:
            segments.append((mode, "\n".join(buf)))
            buf = []
        mode = this
        buf.append(ln)
    if buf:
        segments.append((mode, "\n".join(buf)))
    return segments


def chunk_document(meta: DocumentMeta, text: str, cfg: ChunkingConfig,
                   source_mode: str = "native",
                   ocr_confidence: Optional[float] = None) -> list[ChunkPayload]:
    # token target ≈ words * 0.75 → words ≈ target_tokens / 0.75
    target_words = int(cfg.target_tokens / 0.75)
    overlap_words = int(cfg.overlap_tokens / 0.75)
    chunks: list[ChunkPayload] = []
    source_fields = _source_fields(meta)

    def _mk(chunk_id: str, section: str, page: Optional[int], norm: str,
            quality_flags: Optional[list[str]] = None) -> ChunkPayload:
        return ChunkPayload(
            chunk_id=chunk_id, doc_id=meta.doc_id, section=section, page=page,
            text=norm,
            chunk_hash="sha256:" + hashlib.sha256(norm.encode()).hexdigest(),
            language=meta.language, sensitivity=meta.sensitivity,
            allowed_roles=meta.allowed_roles,
            allowed_purposes=meta.allowed_purposes,
            kg_node_ids=KG_HINTS.get(meta.doc_id, []),
            source_mode=source_mode, ocr_confidence=ocr_confidence,
            quality_flags=quality_flags or [],
            **source_fields)

    for s_no, (title, body) in enumerate(_sections(text), 1):
        page = None
        pg = re.search(r"<<<PAGE (\d+)>>>", body)
        if pg:
            page = int(pg.group(1))
            body = re.sub(r"<<<PAGE \d+>>>", "", body)

        if cfg.keep_tables_intact:
            segments = _split_out_tables(body)
        else:
            segments = [("text", body)]

        c_no = 0
        t_no = 0
        for kind, seg in segments:
            if kind == "table":
                # Emit the whole table as ONE atomic chunk, never split
                # across the token budget — a split eligibility/assessment
                # table is worse than a long one, since half a table cited
                # as evidence is actively misleading. Whitespace inside is
                # preserved (row structure is the content).
                t_no += 1
                table_text = seg.strip()
                if len(table_text) < 40:
                    continue
                flags = []
                if len(table_text.split()) > target_words * 2:
                    flags.append("oversized_table_chunk")
                chunks.append(_mk(f"{meta.doc_id}#s{s_no}-t{t_no}", title, page,
                                  table_text, quality_flags=flags))
                continue
            for piece in _split_words(seg, target_words, overlap_words):
                norm = re.sub(r"\s+", " ", piece).strip()
                if len(norm) < 40:      # skip trivial fragments
                    continue
                c_no += 1
                chunks.append(_mk(f"{meta.doc_id}#s{s_no}-c{c_no}", title, page, norm))
    return chunks


# ------------------------------------------------------------------ register
def load_register() -> list[DocumentMeta]:
    metas = []
    with (CORPUS / "SOURCE_REGISTER.csv").open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("status", "").strip() == "TO_DOWNLOAD":
                continue  # not yet fetched — never ingest what we don't have
            try:
                metas.append(DocumentMeta(
                    doc_id=row["doc_id"].strip(),
                    title=row["title"].strip(),
                    source_org=row["source_org"].strip(),
                    source_url=row.get("source_url", "").strip() or None,
                    doc_type=row["doc_type"].strip(),
                    sector=row.get("sector", "healthcare").strip(),
                    language=row.get("language", "en").strip(),
                    version=row.get("version", "1.0").strip(),
                    license=row.get("license", "public").strip(),
                    last_updated=row.get("last_updated", "").strip() or None,
                    sensitivity=row.get("sensitivity", "public").strip()))
            except Exception as e:  # fail closed, loudly
                raise SystemExit(
                    f"REGISTER INVALID for doc_id={row.get('doc_id')}: {e}")
    return metas


def _find_raw(doc_id: str) -> Path | None:
    exts = (".md", ".txt", ".pdf", ".html", ".htm", ".srt", ".vtt") + tuple(_IMAGE_SUFFIXES)
    for ext in exts:
        p = CORPUS / "raw" / f"{doc_id}{ext}"
        if p.exists():
            return p
    return None


# ------------------------------------------------------------------- exports
def export_bm25(chunks: list[ChunkPayload], metas: dict[str, DocumentMeta]) -> int:
    """Write phase6e chunk-store JSONL so the reused BM25 leg indexes us."""
    out = DATA_DIR / "corpora" / AGENT_CORPUS_ID
    out.mkdir(parents=True, exist_ok=True)
    with (out / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            m = metas[c.doc_id]
            f.write(json.dumps({
                "agent_id": AGENT_CORPUS_ID,
                "chunk_id": c.chunk_id,
                "title": f"{m.title} — {c.section}"[:120],
                "body": c.text,
                "language": "hi-IN" if c.language == "hi" else "en-IN",
                # Sourced from the chunk's own denormalized attribution
                # fields (captured at ingest time), not re-joined via metas
                # — the chunk is the single source of truth for provenance.
                "source": f"{c.source_org} ({c.doc_id} v{m.version})",
                "source_url": c.source_url or "",
                "last_verified": c.source_last_updated or "",
                "verified_by": "substrate-ingest",
                "tags": [m.doc_type, m.sector],
                "state_code": "central",
                "uploaded_by": "substrate-ingest",
                "metadata": {          # governance payload for RBAC post-filter
                    "doc_id": c.doc_id, "section": c.section, "page": c.page,
                    "chunk_hash": c.chunk_hash,
                    "sensitivity": c.sensitivity.value,
                    "allowed_roles": [r.value for r in c.allowed_roles],
                    "allowed_purposes": [p.value for p in c.allowed_purposes],
                    "kg_node_ids": c.kg_node_ids,
                    "source_mode": c.source_mode,
                    "ocr_confidence": c.ocr_confidence,
                    "quality_flags": c.quality_flags,
                },
            }, ensure_ascii=False) + "\n")
    return len(chunks)


# ------------------------------------------------- dedup + quality tagging
_SHORT_CHUNK_CHARS = 80        # tag borderline-short chunks (hard floor is 40, enforced earlier)


def _normalized_text_hash(text: str) -> str:
    """Coarser normalisation than chunk_hash (lowercase, strip punctuation)
    — catches near-duplicates that differ only in punctuation/casing (e.g.
    the same passage ingested once natively and once via OCR)."""
    norm = re.sub(r"[^\w\s]", "", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _dedup_and_tag(chunks: list[ChunkPayload], cfg: ChunkingConfig) -> list[ChunkPayload]:
    """Exact duplicates (identical chunk_hash, anywhere in the corpus — same
    passage re-appearing in a re-uploaded revision, or two docs quoting the
    same clause) are dropped, keeping the first occurrence. Near-duplicates
    (same text modulo case/punctuation — e.g. OCR vs. native extraction of
    the same passage) are kept but flagged, since they may still carry
    distinct citation value (different doc_id/section).

    No-op (tags nothing, drops nothing) when cfg.dedup_enabled is False —
    the flag is part of the manifest hash, so that's an explicit, auditable
    choice, not silent behavior.
    """
    if not cfg.dedup_enabled:
        return chunks

    seen_exact: dict[str, str] = {}       # chunk_hash -> first chunk_id
    seen_normalized: dict[str, str] = {}  # normalized hash -> first chunk_id
    out: list[ChunkPayload] = []
    dropped = 0

    for c in chunks:
        if c.chunk_hash in seen_exact:
            dropped += 1
            log.info("dedup: dropping exact duplicate %s (matches %s)",
                     c.chunk_id, seen_exact[c.chunk_hash])
            continue
        seen_exact[c.chunk_hash] = c.chunk_id

        norm_hash = _normalized_text_hash(c.text)
        if norm_hash in seen_normalized and seen_normalized[norm_hash] != c.chunk_id:
            c.quality_flags.append(f"possible_near_duplicate_of:{seen_normalized[norm_hash]}")
        else:
            seen_normalized[norm_hash] = c.chunk_id

        if len(c.text) < _SHORT_CHUNK_CHARS:
            c.quality_flags.append("short_chunk")
        if c.source_mode == "ocr" and c.ocr_confidence is not None and c.ocr_confidence < 0.6:
            c.quality_flags.append("ocr_low_confidence")
        if c.source_mode == "ocr" and not c.text.strip():
            c.quality_flags.append("ocr_yielded_no_text")

        out.append(c)

    if dropped:
        log.info("dedup: dropped %d exact-duplicate chunk(s) out of %d", dropped, len(chunks))
    return out


# ---------------------------------------------------------------------- main
def run(qdrant: bool = False, qdrant_path: Optional[str] = "data/qdrant_local",
        qdrant_url: Optional[str] = None, embedding_provider: Optional[str] = None,
        embed_kg: bool = False) -> IndexManifest:
    cfg = ChunkingConfig()
    metas = load_register()
    by_id = {m.doc_id: m for m in metas}
    all_chunks: list[ChunkPayload] = []
    skipped = []
    for meta in metas:
        raw = _find_raw(meta.doc_id)
        if raw is None:
            skipped.append(meta.doc_id)
            continue
        if raw.suffix.lower() in (".srt", ".vtt"):
            chunks = chunk_transcript(meta, raw.read_text(encoding="utf-8", errors="replace"), cfg)
        elif cfg.ocr_enabled and raw.suffix.lower() in _IMAGE_SUFFIXES:
            text, conf = _ocr_extract_text(raw, language_hint=f"{meta.language}-IN")
            chunks = chunk_document(meta, text, cfg, source_mode="ocr", ocr_confidence=conf)
        else:
            native_text = _extract_text(raw)
            if cfg.ocr_enabled and _needs_ocr(raw, native_text):
                log.info("%s: low/no text layer detected — routing through Sarvam Vision OCR",
                         meta.doc_id)
                ocr_text, conf = _ocr_extract_text(raw, language_hint=f"{meta.language}-IN")
                if ocr_text.strip():
                    chunks = chunk_document(meta, ocr_text, cfg, source_mode="ocr", ocr_confidence=conf)
                else:
                    log.warning("%s: OCR yielded no text — keeping thin native extraction", meta.doc_id)
                    chunks = chunk_document(meta, native_text, cfg)
            else:
                chunks = chunk_document(meta, native_text, cfg)
        quarantined = sum(quarantine_scan(c) for c in chunks)
        log.info("%s: %d chunks from %s%s", meta.doc_id, len(chunks), raw.name,
                 f" ({quarantined} QUARANTINED)" if quarantined else "")
        all_chunks.extend(chunks)
    if skipped:
        log.warning("no raw file yet (register says available): %s", skipped)
    if not all_chunks:
        raise SystemExit("no chunks produced — nothing in corpus/raw matches register")

    before_dedup = len(all_chunks)
    all_chunks = _dedup_and_tag(all_chunks, cfg)
    if len(all_chunks) != before_dedup:
        log.info("corpus: %d chunks after dedup (was %d)", len(all_chunks), before_dedup)

    embedding_model, dim = "bm25-only", 0
    vs = None
    if qdrant:
        from .vector_store import VectorStore, get_embedder
        embed, dim, embedding_model = get_embedder(embedding_provider)
        vs = VectorStore(embed, dim, url=qdrant_url, path=qdrant_path)
        vs.ensure_collection()

    manifest = IndexManifest(embedding_model=embedding_model, embedding_dim=dim,
                             chunking_config=cfg,
                             doc_count=len(metas) - len(skipped))
    manifest.finalise([c.chunk_hash for c in all_chunks])
    for c in all_chunks:
        c.index_manifest_id = manifest.manifest_id

    curated = CORPUS / "curated"
    curated.mkdir(exist_ok=True)
    with (curated / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(c.model_dump_json() + "\n")

    export_bm25(all_chunks, by_id)
    if qdrant:
        vs.upsert_chunks(all_chunks)
        if embed_kg:
            from .kg_embed import embed_and_store_kg_nodes, KG_COLLECTION
            kg_vs = VectorStore(vs.embed, dim, collection=KG_COLLECTION, client=vs.client)
            kg_vs.ensure_collection()
            manifest.kg_node_count = embed_and_store_kg_nodes(kg_vs, manifest_id=manifest.manifest_id)

    ManifestRegistry(DATA_DIR).save(manifest)
    log.info("manifest %s: %d docs, %d chunks", manifest.manifest_id,
             manifest.doc_count, manifest.chunk_count)
    return manifest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    m = run(qdrant="--qdrant" in sys.argv, embed_kg="--embed-kg" in sys.argv)
    print(json.dumps(json.loads(m.model_dump_json()), indent=1))
