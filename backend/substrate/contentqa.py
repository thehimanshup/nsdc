"""Content QA assistant — coverage checks & tagged item drafts (ST-803).

Two SME capabilities, both evidence-first:

  coverage_check(course_id, qp_code)
      NOS-level gap analysis: for every NOS in the QP (from the curated KG
      seed), scan the course's corpus chunks for coverage evidence. Output
      is a per-NOS covered/gap table with the exact chunk ids used —
      deterministic and auditable (no LLM required).

  draft_items(nos_code, count, bloom_max)
      Assessment item drafts tagged {qp, nos, bloom} with
      review_status='pending'. LLM-composed when a live provider exists;
      deterministic template items in mock/offline mode. SME approves via
      the same maker-checker pattern as draft notes.
"""
from __future__ import annotations

import csv
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()
_KG_CURATED = Path(__file__).parent / "kg" / "curated"

BLOOM_LABELS = {1: "Remember", 2: "Understand", 3: "Apply",
                4: "Analyze", 5: "Evaluate", 6: "Create"}

_STOP = {"with", "and", "the", "per", "for", "of", "to", "in"}


def _rows(name: str) -> list[dict]:
    p = _KG_CURATED / name
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _course_chunks(course_doc_id: str, qp_doc_hint: str = "") -> list[dict]:
    """Chunks belonging to the course doc (and optionally its QP doc)."""
    chunks_file = Path(__file__).resolve().parents[2] / "corpus" / "curated" / "chunks.jsonl"
    if not chunks_file.exists():
        return []
    out = []
    for line in chunks_file.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if c["doc_id"] in (course_doc_id, qp_doc_hint):
            out.append(c)
    return out


def _keywords(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in _STOP]


def coverage_check(course_id: str, qp_code: str) -> dict:
    courses = {r["id"]: r for r in _rows("nodes_course.csv")}
    course = courses.get(course_id)
    if course is None:
        return {"error": f"unknown course_id '{course_id}' — known: {sorted(courses)}"}
    nos_rows = [r for r in _rows("nodes_nos.csv") if r["qp_code"] == qp_code]
    if not nos_rows:
        return {"error": f"no NOS found for qp_code '{qp_code}'"}

    qp_doc = next((r["source_doc_id"] for r in _rows("nodes_qp.csv")
                   if r["qp_code"] == qp_code), "")
    chunks = _course_chunks(course["source_doc_id"], qp_doc)
    covered, gaps = [], []
    for nos in nos_rows:
        kws = _keywords(nos["title"])
        evidence = []
        for c in chunks:
            text = c["text"].lower()
            hits = sum(1 for k in kws if k in text)
            if nos["nos_code"].lower() in text or (kws and hits / len(kws) >= 0.5):
                evidence.append(c["chunk_id"])
        entry = {"nos_code": nos["nos_code"], "title": nos["title"],
                 "evidence_chunks": evidence[:5]}
        (covered if evidence else gaps).append(entry)

    declared = course["covers_qp"] == qp_code
    return {
        "course_id": course_id, "course_title": course["title"],
        "qp_code": qp_code, "declared_coverage": declared,
        "nos_total": len(nos_rows), "nos_covered": len(covered),
        "covered": covered, "gaps": gaps,
        "verdict": (f"{len(covered)}/{len(nos_rows)} NOS evidenced in course "
                    f"material{' — declared aligned' if declared else ' — NOT declared for this QP'}."
                    + (f" Gaps: {', '.join(g['nos_code'] for g in gaps)}." if gaps else "")),
        "review_status": "pending",
        "note": "Deterministic keyword-evidence check on seed corpus — SME must verify.",
    }


# ------------------------------------------------------------------ items
def _items_path(data_dir) -> Path:
    return Path(data_dir) / "assessment_items.json"


_TEMPLATES_BY_BLOOM = {
    1: "List the key steps involved in: {title}.",
    2: "Explain why the following practice matters in daily work: {title}.",
    3: "Given a typical ward scenario, demonstrate how you would carry out: {title}.",
    4: "A colleague performs '{title}' incorrectly. Identify the errors and their risks.",
}


async def draft_items(nos_code: str, count: int, bloom_max: int,
                      author: str, data_dir: str | Path = "data",
                      llm=None) -> dict:
    nos = next((r for r in _rows("nodes_nos.csv") if r["nos_code"] == nos_code), None)
    if nos is None:
        return {"error": f"unknown nos_code '{nos_code}'"}
    count = max(1, min(count, 10))
    bloom_max = max(1, min(bloom_max, 6))

    drafts, mode = [], "template"
    if llm is not None:
        try:
            raw = await llm.chat_complete(messages=[
                {"role": "system", "content":
                 "You write vocational assessment questions. Respond ONLY with a "
                 "JSON list of strings, one question per string. Questions must "
                 "be answerable from standard training on the given standard."},
                {"role": "user", "content":
                 f"Write {count} assessment questions for the National "
                 f"Occupational Standard '{nos['title']}' ({nos_code}), "
                 f"across Bloom levels 1-{bloom_max}."}],
                temperature=0.4, max_tokens=600)
            m = re.search(r"\[.*\]", raw, re.S)
            texts = json.loads(m.group(0)) if m else []
            if texts:
                drafts = [str(t)[:400] for t in texts[:count]]
                mode = "llm"
        except Exception:
            pass
    if not drafts:  # deterministic fallback (mock/offline)
        blooms = [b for b in range(1, bloom_max + 1) if b in _TEMPLATES_BY_BLOOM]
        drafts = [_TEMPLATES_BY_BLOOM[blooms[i % len(blooms)]].format(title=nos["title"])
                  for i in range(count)]

    items = []
    for i, text in enumerate(drafts):
        bloom = min((i % bloom_max) + 1, bloom_max)
        items.append({
            "item_id": "itm_" + uuid.uuid4().hex[:10],
            "text": text,
            "qp_code": nos["qp_code"], "nos_code": nos_code,
            "bloom_level": bloom, "bloom_label": BLOOM_LABELS[bloom],
            "review_status": "pending", "compose_mode": mode,
            "created_by": author,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    with _LOCK:
        p = _items_path(data_dir)
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        existing.extend(items)
        p.write_text(json.dumps(existing, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"items": items, "compose_mode": mode,
            "note": "All items review_status=pending — SME sign-off required "
                    "before any learner-facing use (RFP 4.B.8)."}


def list_items(data_dir: str | Path = "data") -> list[dict]:
    p = _items_path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
