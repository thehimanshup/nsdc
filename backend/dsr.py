"""Data Subject Rights (DSR) handlers per DPDP Act 2023 §11-§14.

Implements:
  - Right to information (§11) — citizen sees what we hold about them
  - Right to access (§12)      — citizen can download a copy
  - Right to correction (§12)  — citizen requests a field correction
  - Right to erasure (§12)     — citizen requests deletion subject to legal exemptions
  - Right to grievance redressal (§13) — handled via /cmo/create_grievance tool

DPDP §17(3): erasure can be refused when retention is required by other
law (criminal records, tax records, ongoing legal proceedings, audit
trail). We honour erasure for chat content + uploads, but the audit log
and consent ledger persist (those ARE required by other law / good
governance).
"""
from __future__ import annotations

import io
import json
import logging
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import audit, consent
from .config import settings
from .store import store

log = logging.getLogger("dsr")


_REQUESTS_FILE = Path(settings.data_dir) / "dsr_requests.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_request(record: dict) -> None:
    """Persist DSR request for the DPO's audit trail."""
    _REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_REQUESTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# §11 + §12 — Right to access (export)
# ---------------------------------------------------------------------------

def export_citizen_data(citizen_id: str) -> bytes:
    """Generate a ZIP archive of all data held about this citizen.

    Returned bytes can be written to disk or streamed to HTTP response.
    """
    citizen = store.get_citizen(citizen_id) or {}

    # 1. Profile
    profile = {
        "citizenId": citizen_id,
        "msisdn_last4": citizen.get("msisdn", "")[-4:],   # never export full MSISDN
        "preferred_language": citizen.get("language", "en-IN"),
        "created_at": citizen.get("createdAt", ""),
        "data_residency": "india_only",
    }

    # 2. Conversations — all messages across all agents
    convs = {}
    for conv_id, meta in store.conv_meta.items():
        if meta.get("citizenId") != citizen_id:
            continue
        agent_id = meta["agentId"]
        msgs = store.conversations.get(conv_id, [])
        convs[agent_id] = [
            {
                "id": m.id, "role": m.role, "type": m.type,
                "text": m.text, "lang": m.lang,
                "ts": m.timestamp.isoformat() if m.timestamp else "",
                "channel": getattr(m, "channel", "simulator"),
                "audioUrl": m.audioUrl, "mediaUrl": m.mediaUrl,
            }
            for m in msgs
        ]

    # 3. Consents
    consents = consent.list_all_for_citizen(citizen_id)
    consents_export = [
        {
            "requestId": e["requestId"],
            "grantId": e.get("grantId"),
            "agentId": e["agentId"],
            "toolId": e["toolId"],
            "scope": e["scope"],
            "purpose": e["purpose"],
            "status": e["status"],
            "requestedAt": e["requestedAt"],
            "decidedAt": e.get("decidedAt"),
            "expiresAt": e["expiresAt"],
        }
        for e in consents
    ]

    # 4. Audit trail (events that reference this citizen)
    audit_entries = audit.query_for_citizen(citizen_id)

    # 4b. Phase 6e — casework records (grievances, scheme applications,
    # project reports, service requests). DPDP §12 right-to-access must
    # include these or the export is incomplete.
    records_export = []
    try:
        from .records.store import records_store
        records_export = [r.public_view() for r in records_store.for_citizen(citizen_id)]
    except Exception as e:
        log.warning("DSR: could not collect records for %s: %s", citizen_id, e)

    # 5. Build the ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", _readme_text())
        zf.writestr("profile.json", json.dumps(profile, indent=2, ensure_ascii=False))
        zf.writestr("conversations.json", json.dumps(convs, indent=2,
                                                     ensure_ascii=False, default=str))
        zf.writestr("consents.json", json.dumps(consents_export, indent=2,
                                                 ensure_ascii=False))
        zf.writestr("records.json", json.dumps(records_export, indent=2,
                                               ensure_ascii=False, default=str))
        zf.writestr("audit_trail.json", json.dumps(audit_entries, indent=2,
                                                     ensure_ascii=False))
        # 6. Bundle media + audio files the citizen uploaded / received
        media_count = 0
        for conv_id, meta in store.conv_meta.items():
            if meta.get("citizenId") != citizen_id:
                continue
            for m in store.conversations.get(conv_id, []):
                for url in (m.mediaUrl, m.audioUrl):
                    if not url:
                        continue
                    fname = os.path.basename(url.split("?")[0])
                    if not fname:
                        continue
                    for subdir in ("audio", "uploads"):
                        p = Path(settings.data_dir) / subdir / fname
                        if p.exists():
                            try:
                                zf.write(p, f"{subdir}/{fname}")
                                media_count += 1
                            except Exception as e:
                                log.warning("Failed to add %s to export: %s", p, e)
                            break

    audit.append_event(
        actor=citizen_id, action="dsr.export",
        resource={"citizenId": citizen_id},
        payload={"conversations": sum(len(v) for v in convs.values()),
                 "consents": len(consents_export),
                 "audit_entries": len(audit_entries),
                 "media_files": media_count},
    )
    _append_request({
        "ts": _now(), "type": "export", "citizenId": citizen_id,
        "stats": {"conversations": sum(len(v) for v in convs.values()),
                  "consents": len(consents_export)},
    })
    log.info("DSR export prepared for %s: %d msgs, %d consents, %d audit, %d media",
             citizen_id, sum(len(v) for v in convs.values()),
             len(consents_export), len(audit_entries), media_count)
    return buf.getvalue()


def _readme_text() -> str:
    return f"""# Your data — Personal data export

Generated: {_now()}

This archive contains every piece of personal data we hold about you,
as required by the Digital Personal Data Protection Act, 2023 (§12).

## Files in this archive

- `profile.json` — Your account profile (citizen identifier, preferred
  language, masked MSISDN — full MSISDN is never exported).
- `conversations.json` — Every message you've sent or received, grouped
  by the government department agent.
- `consents.json` — Every consent grant or refusal you've recorded, with
  scope, purpose, decision timestamp, and expiry.
- `audit_trail.json` — Every system action that referenced you (tool
  calls, consent decisions, broadcasts). Hash-chained and signature-
  verifiable.
- `audio/` and `uploads/` — Voice notes and document images you uploaded.

## Your rights

If you believe any data here is incorrect, request a correction via
the platform's `My Data → Correct` flow or by writing to the Data
Protection Officer at the contact listed in the platform's privacy
notice. You may also request erasure subject to legal exemptions
(audit logs and consent records are retained per applicable law).
"""


# ---------------------------------------------------------------------------
# §12 — Right to correction
# ---------------------------------------------------------------------------

def submit_correction(citizen_id: str, field: str, new_value: str,
                      reason: str = "") -> dict:
    """Record a correction request. DPO reviews and applies offline."""
    req_id = f"dsr_corr_{uuid.uuid4().hex[:10]}"
    record = {
        "id": req_id, "ts": _now(), "type": "correction",
        "citizenId": citizen_id, "field": field[:64],
        "new_value": new_value[:200], "reason": reason[:300],
        "status": "received",
    }
    _append_request(record)
    audit.append_event(
        actor=citizen_id, action="dsr.correction.submit",
        resource={"citizenId": citizen_id, "field": field},
        payload={"reason_preview": reason[:80]},
    )
    log.info("DSR correction request %s from %s on field %s", req_id, citizen_id, field)
    return {"ok": True, "requestId": req_id,
            "status": "received",
            "sla": "DPO will respond within 30 working days per DPDP §13."}


# ---------------------------------------------------------------------------
# §12 — Right to erasure
# ---------------------------------------------------------------------------

def submit_erasure(citizen_id: str, reason: str = "") -> dict:
    """Erase citizen's chat content + uploads. Audit trail + consent ledger
    are retained per DPDP §17(3) (legal record-keeping requirement)."""
    req_id = f"dsr_eras_{uuid.uuid4().hex[:10]}"
    citizen = store.get_citizen(citizen_id)
    if not citizen:
        return {"ok": False, "error": "citizen not found"}

    # 1. Anonymise messages — keep the row (audit) but blank out content
    erased_msgs = 0
    for conv_id, meta in store.conv_meta.items():
        if meta.get("citizenId") != citizen_id:
            continue
        for m in store.conversations.get(conv_id, []):
            if m.text and m.text != "[ERASED]":
                m.text = "[ERASED]"
                erased_msgs += 1
            m.mediaUrl = None
            m.audioUrl = None

    # 2. Delete media + audio files
    erased_files = 0
    for subdir in ("uploads", "audio"):
        d = Path(settings.data_dir) / subdir
        if not d.exists():
            continue
        # We can't perfectly identify which files belong to this citizen without
        # storing ownership. For Phase 6 we delete files that were referenced
        # by this citizen's messages. (Simpler approach.)
        for conv_id, meta in store.conv_meta.items():
            if meta.get("citizenId") != citizen_id:
                continue
            for m in store.conversations.get(conv_id, []):
                for url in (m.mediaUrl, m.audioUrl):
                    if not url:
                        continue
                    fname = os.path.basename(url.split("?")[0])
                    p = d / fname
                    if p.exists():
                        try:
                            p.unlink()
                            erased_files += 1
                        except Exception:
                            pass

    # 2b. Phase 6e — anonymise casework records. We keep the row (record id
    # + status timeline) because grievances/applications are legal records
    # retained under DPDP §17(3), but blank the citizen-supplied free text
    # (title, description, notes) and the documents captured at filing time.
    erased_records = 0
    try:
        from .records.store import records_store
        for rec in records_store.for_citizen(citizen_id):
            changed = False
            if rec.title and rec.title != "[ERASED]":
                rec.title = "[ERASED]"; changed = True
            if rec.description and rec.description != "[ERASED]":
                rec.description = "[ERASED]"; changed = True
            if rec.documents:
                rec.documents = []; changed = True
            if rec.attachments:
                rec.attachments = []; changed = True
            for ev in rec.timeline:
                if ev.get("note") and ev["note"] != "[ERASED]":
                    ev["note"] = "[ERASED]"; changed = True
            if rec.extra:
                rec.extra = {"erased": True}; changed = True
            if changed:
                rec.extra = {"erased": True}
                records_store.save(rec)
                erased_records += 1
    except Exception as e:
        log.warning("DSR: could not erase records for %s: %s", citizen_id, e)

    # 3. Mark the citizen profile
    citizen["erasure_requested_at"] = _now()
    citizen["erased"] = True

    # 4. Persist + audit
    store._persist()
    record = {
        "id": req_id, "ts": _now(), "type": "erasure", "citizenId": citizen_id,
        "reason": reason[:300], "status": "completed",
        "stats": {"messages_erased": erased_msgs, "files_deleted": erased_files,
                  "records_anonymised": erased_records},
    }
    _append_request(record)
    audit.append_event(
        actor=citizen_id, action="dsr.erasure.complete",
        resource={"citizenId": citizen_id},
        payload=record["stats"],
    )
    log.info("DSR erasure %s for %s: %d msgs, %d files",
             req_id, citizen_id, erased_msgs, erased_files)
    return {
        "ok": True, "requestId": req_id, "status": "completed",
        "stats": record["stats"],
        "note": ("Audit trail and consent ledger entries are retained per "
                 "DPDP §17(3) record-keeping requirements. Your messages "
                 "and uploads are erased."),
    }


# ---------------------------------------------------------------------------
# Helpers — list DSR requests (for DPO admin dashboard)
# ---------------------------------------------------------------------------

def list_dsr_requests() -> list[dict]:
    if not _REQUESTS_FILE.exists():
        return []
    out: list[dict] = []
    with open(_REQUESTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return list(reversed(out))[:200]
