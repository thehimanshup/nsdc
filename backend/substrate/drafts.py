"""Draft-note maker-checker (ST-703, PRD US-2.2, RFP 4.B.4.2).

Officers generate scheme-status DRAFT notes compiled from their scoped
analytics. Drafts are never sent anywhere by the system:

    pending  --approve-->  approved   (human decision, audited)
             --reject--->  rejected   (human decision, audited)

Storage: JSON file (data/draft_notes.json), same pattern as phase6e
stores; swap to Postgres at delivery.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .analytics import run_analytics
from .authn import Principal

_LOCK = threading.RLock()

WATERMARK = ("⚠ AI-GENERATED DRAFT — requires human review before any "
             "circulation. Compiled from the synthetic transactional event "
             "store; verify every figure.")


def _store_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "draft_notes.json"


def _load(data_dir) -> dict:
    p = _store_path(data_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save(data_dir, drafts: dict) -> None:
    _store_path(data_dir).write_text(
        json.dumps(drafts, indent=1, ensure_ascii=False), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_note(principal: Principal, data_dir: str | Path = "data",
                  subject: str = "Monthly scheme status") -> dict:
    """Compile a status note from the officer's OWN scoped analytics."""
    sections = []
    for q, heading in [("enrolment summary", "Enrolment"),
                       ("certification funnel", "Certification funnel"),
                       ("low attendance", "Attendance watchlist"),
                       ("dropout", "Dropout"),
                       ("placement", "Placement outcomes")]:
        r = run_analytics(q, principal, data_dir)
        if not r.refused and r.rows:
            sections.append(f"### {heading}\n{r.answer_text.split(chr(10)+chr(10))[0]}")
    scope = principal.jurisdiction.get("district", "n/a")
    body = (f"## DRAFT: {subject}\n\n{WATERMARK}\n\n"
            f"**Scheme:** PMKVY 4.0 · **Jurisdiction:** {scope} · "
            f"**Prepared for:** {principal.name}\n\n" + "\n\n".join(sections))
    draft = {
        "draft_id": "note_" + uuid.uuid4().hex[:10],
        "subject": subject,
        "body_markdown": body,
        "created_by": principal.sub,
        "jurisdiction": principal.jurisdiction,
        "review_status": "pending",
        "created_at": _now(),
        "decided_at": None,
        "decided_by": None,
        "decision_comment": None,
    }
    with _LOCK:
        drafts = _load(data_dir)
        drafts[draft["draft_id"]] = draft
        _save(data_dir, drafts)
    return draft


def list_notes(principal: Principal, data_dir: str | Path = "data") -> list[dict]:
    with _LOCK:
        drafts = list(_load(data_dir).values())
    if principal.role.value == "admin":
        return drafts
    return [d for d in drafts if d["created_by"] == principal.sub]


def decide(principal: Principal, draft_id: str, decision: str,
           comment: str = "", data_dir: str | Path = "data") -> Optional[dict]:
    """Approve/reject — the human (checker) decision. Only the creating
    officer or an admin may decide; a decided draft is immutable."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved|rejected")
    with _LOCK:
        drafts = _load(data_dir)
        d = drafts.get(draft_id)
        if d is None:
            return None
        if d["created_by"] != principal.sub and principal.role.value != "admin":
            raise PermissionError("only the creating officer or admin may decide")
        if d["review_status"] != "pending":
            raise ValueError(f"draft already {d['review_status']} — immutable")
        d.update(review_status=decision, decided_at=_now(),
                 decided_by=principal.sub, decision_comment=comment[:500])
        _save(data_dir, drafts)
        return d
