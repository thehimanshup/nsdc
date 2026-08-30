"""Consent ledger — append-only, hash-chained, Ed25519/HMAC-signed.

Replaces the in-memory consent.py from Phase 2.

Every consent request and decision is logged as a ledger entry. Entries
are linked by prevHash → thisHash, so reordering / editing / deleting
any past entry breaks the chain and verify_chain() returns the broken
entry number.

Public API matches Phase 2 (drop-in replacement):
    create_request(...) → ConsentRequest
    decide(...) → ConsentRequest | None
    get_request(...)
    get_grant(...)
    list_active_for_citizen(...)
    verify_chain() → {ok, entries_checked, ...}
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from .crypto_utils import canonical_json, sha256_hex, sign_bytes, verify_signature

log = logging.getLogger("consent.ledger")

_LOCK = threading.RLock()
_LEDGER_FILE = Path(settings.data_dir) / "consent_ledger.jsonl"
_REQUESTS: dict[str, dict] = {}      # request_id -> latest state
_GRANTS: dict[str, dict] = {}        # grant_id -> the entry where it was granted
_BY_CITIZEN_ACTIVE: dict[str, list[str]] = {}   # citizen_id -> [request_ids with granted+unexpired]
_last_hash: str = "GENESIS"
_entry_counter: int = 0


# ---------------------------------------------------------------------------
# Backward-compatible dataclass — same shape Phase 2 callers expect
# ---------------------------------------------------------------------------

@dataclass
class ConsentRequest:
    request_id: str
    citizen_id: str
    agent_id: str
    tool_id: str
    scope: str
    purpose: str
    requested_at: datetime
    expires_at: datetime
    status: str = "pending"
    decided_at: Optional[datetime] = None
    grant_id: Optional[str] = None


def _from_entry(e: dict) -> ConsentRequest:
    return ConsentRequest(
        request_id=e["requestId"],
        citizen_id=e["citizenId"],
        agent_id=e["agentId"],
        tool_id=e["toolId"],
        scope=e["scope"],
        purpose=e["purpose"],
        requested_at=datetime.fromisoformat(e["requestedAt"].replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(e["expiresAt"].replace("Z", "+00:00")),
        status=e["status"],
        decided_at=datetime.fromisoformat(e["decidedAt"].replace("Z", "+00:00"))
                   if e.get("decidedAt") else None,
        grant_id=e.get("grantId"),
    )


# ---------------------------------------------------------------------------
# Ledger init — load existing entries on startup
# ---------------------------------------------------------------------------

def init_ledger() -> None:
    """Replay the ledger to populate in-memory caches."""
    global _last_hash, _entry_counter
    _REQUESTS.clear()
    _GRANTS.clear()
    _BY_CITIZEN_ACTIVE.clear()
    if not _LEDGER_FILE.exists():
        _last_hash = "GENESIS"
        _entry_counter = 0
        log.info("Consent ledger: starting fresh (no existing %s)", _LEDGER_FILE)
        return
    n = 0
    with open(_LEDGER_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            _REQUESTS[e["requestId"]] = e
            if e.get("grantId"):
                _GRANTS[e["grantId"]] = e
            n += 1
            _last_hash = e.get("thisHash", _last_hash)
            _entry_counter = e.get("entryNo", _entry_counter)
    # Rebuild active-grants index
    now = datetime.now(timezone.utc)
    for req_id, e in _REQUESTS.items():
        if e.get("status") == "granted":
            exp = datetime.fromisoformat(e["expiresAt"].replace("Z", "+00:00"))
            if exp >= now:
                _BY_CITIZEN_ACTIVE.setdefault(e["citizenId"], []).append(req_id)
    log.info("Consent ledger: loaded %d entries; last hash = %s",
             n, _last_hash[:16] + "…")


# ---------------------------------------------------------------------------
# Append helper
# ---------------------------------------------------------------------------

def _append_entry(body_no_hash: dict) -> dict:
    """Hash + sign + persist a ledger entry. Updates _last_hash and counter."""
    global _last_hash, _entry_counter
    with _LOCK:
        _entry_counter += 1
        body = dict(body_no_hash)
        body["entryNo"] = _entry_counter
        body["prevHash"] = _last_hash
        this_hash = sha256_hex(canonical_json(body))
        body["thisHash"] = this_hash
        body["signature"] = sign_bytes(this_hash.encode())
        try:
            _LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error("Failed to persist ledger entry: %s", e)
        _last_hash = this_hash
    return body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_request(*, citizen_id: str, agent_id: str, tool_id: str,
                   scope: str, purpose: str, ttl_seconds: int = 300) -> ConsentRequest:
    """Record a new consent request (status='pending')."""
    now = datetime.now(timezone.utc)
    req_id = f"creq_{uuid.uuid4().hex[:12]}"
    body = {
        "type": "consent.request",
        "requestId": req_id,
        "citizenId": citizen_id,
        "agentId": agent_id,
        "toolId": tool_id,
        "scope": scope,
        "purpose": purpose,
        "requestedAt": now.isoformat(timespec="seconds"),
        "expiresAt": (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        "status": "pending",
        "ttlSeconds": ttl_seconds,
        "decidedAt": None,
        "grantId": None,
    }
    persisted = _append_entry(body)
    _REQUESTS[req_id] = persisted
    log.info("consent.request %s citizen=%s agent=%s tool=%s",
             req_id, citizen_id, agent_id, tool_id)
    return _from_entry(persisted)


def decide(request_id: str, citizen_id: str, decision: str) -> Optional[ConsentRequest]:
    """Record a granted / denied decision."""
    if decision not in ("granted", "denied"):
        return None
    prev = _REQUESTS.get(request_id)
    if not prev or prev["citizenId"] != citizen_id:
        return None
    if prev["status"] != "pending":
        return _from_entry(prev)
    expires_at = datetime.fromisoformat(prev["expiresAt"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        prev["status"] = "expired"
        # Persist the expiry transition for the audit trail
        _append_entry({
            "type": "consent.expired",
            "requestId": request_id,
            "citizenId": citizen_id,
            "agentId": prev["agentId"],
            "toolId": prev["toolId"],
            "scope": prev["scope"],
            "purpose": prev["purpose"],
            "requestedAt": prev["requestedAt"],
            "expiresAt": prev["expiresAt"],
            "status": "expired",
            "decidedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "grantId": None,
        })
        _REQUESTS[request_id] = {**prev, "status": "expired"}
        return _from_entry(_REQUESTS[request_id])

    body = {
        "type": "consent.decision",
        "requestId": request_id,
        "citizenId": citizen_id,
        "agentId": prev["agentId"],
        "toolId": prev["toolId"],
        "scope": prev["scope"],
        "purpose": prev["purpose"],
        "requestedAt": prev["requestedAt"],
        "expiresAt": prev["expiresAt"],
        "status": decision,
        "decidedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "grantId": (f"cgrant_{uuid.uuid4().hex[:12]}" if decision == "granted" else None),
    }
    persisted = _append_entry(body)
    _REQUESTS[request_id] = persisted
    if persisted.get("grantId"):
        _GRANTS[persisted["grantId"]] = persisted
        _BY_CITIZEN_ACTIVE.setdefault(citizen_id, []).append(request_id)
    log.info("consent.%s %s citizen=%s grantId=%s",
             decision, request_id, citizen_id, persisted.get("grantId"))
    return _from_entry(persisted)


def revoke(grant_id: str, citizen_id: str) -> Optional[ConsentRequest]:
    """Citizen revokes a previously-granted consent (DPDP §6(5))."""
    e = _GRANTS.get(grant_id)
    if not e or e["citizenId"] != citizen_id:
        return None
    if e["status"] != "granted":
        return _from_entry(e)
    body = dict(e)
    body["type"] = "consent.revoked"
    body["status"] = "revoked"
    body["decidedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body.pop("thisHash", None)
    body.pop("prevHash", None)
    body.pop("signature", None)
    body.pop("entryNo", None)
    persisted = _append_entry(body)
    _REQUESTS[e["requestId"]] = persisted
    # Remove from active grants
    actives = _BY_CITIZEN_ACTIVE.get(citizen_id, [])
    if e["requestId"] in actives:
        actives.remove(e["requestId"])
    log.info("consent.revoked grantId=%s citizen=%s", grant_id, citizen_id)
    return _from_entry(persisted)


def get_request(request_id: str) -> Optional[ConsentRequest]:
    e = _REQUESTS.get(request_id)
    return _from_entry(e) if e else None


def get_grant(grant_id: str) -> Optional[ConsentRequest]:
    e = _GRANTS.get(grant_id)
    return _from_entry(e) if e else None


def list_active_for_citizen(citizen_id: str) -> list[ConsentRequest]:
    now = datetime.now(timezone.utc)
    out: list[ConsentRequest] = []
    for req_id in _BY_CITIZEN_ACTIVE.get(citizen_id, []):
        e = _REQUESTS.get(req_id)
        if not e or e.get("status") != "granted":
            continue
        exp = datetime.fromisoformat(e["expiresAt"].replace("Z", "+00:00"))
        if exp >= now:
            out.append(_from_entry(e))
    return out


def list_all_for_citizen(citizen_id: str) -> list[dict]:
    """All ledger entries (any status, any time) for this citizen — used by DSR export."""
    out = []
    for e in _REQUESTS.values():
        if e["citizenId"] == citizen_id:
            out.append(e)
    out.sort(key=lambda e: e.get("entryNo", 0))
    return out


# ---------------------------------------------------------------------------
# Ledger verification
# ---------------------------------------------------------------------------

def verify_chain() -> dict:
    """Walk the consent ledger and verify every hash + signature.

    Returns either {ok: True, entries_checked: N} or {ok: False, broken_at, reason}.
    """
    if not _LEDGER_FILE.exists():
        return {"ok": True, "entries_checked": 0, "note": "ledger empty"}
    expected_prev = "GENESIS"
    checked = 0
    with open(_LEDGER_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "broken_at": checked, "reason": "JSON parse error"}
            checked += 1
            if e.get("prevHash") != expected_prev:
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "prevHash mismatch — entries reordered or deleted"}
            body = {k: v for k, v in e.items() if k not in ("thisHash", "signature")}
            computed = sha256_hex(canonical_json(body))
            if computed != e.get("thisHash"):
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "thisHash mismatch — entry was edited"}
            if not verify_signature(computed.encode(), e.get("signature", "")):
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "signature invalid"}
            expected_prev = e["thisHash"]
    return {"ok": True, "entries_checked": checked,
            "last_hash": expected_prev[:32] + "…" if expected_prev != "GENESIS" else "GENESIS"}
