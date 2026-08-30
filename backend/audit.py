"""Append-only audit log with daily Merkle root.

Every consequential action gets a hash-chained entry. The chain
guarantees that any tampering with past entries (deletion, edit,
reordering) is detectable by walking the chain from genesis.

Format: JSONL at data/audit/events.jsonl
Schema per entry:
    {
      "entryNo": 42,
      "ts": "2026-05-27T08:14:22Z",
      "actor": "ctz_xxx" | "officer-alice" | "system",
      "action": "tool.invoke" | "consent.grant" | "broadcast.send" | ...,
      "resource": {"agentId": "revenue", "toolId": "digilocker.fetch_patta"},
      "payloadHash": "sha256:abc...",  # hash of redacted payload
      "prevHash": "sha256:..." | "GENESIS",
      "thisHash": "sha256:def...",
      "signature": "ed25519:..." | "hmac:...",
    }

Daily Merkle roots written to data/audit/roots/{YYYY-MM-DD}.json. In
production these would be posted to a public transparency log; locally
we just write them to disk so they can be inspected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from .crypto_utils import canonical_json, merkle_root, sha256_hex, sign_bytes, verify_signature
from .pii_redaction import redact_for_logs

log = logging.getLogger("audit")

_LOCK = threading.RLock()
_BUFFER_LOCK = threading.RLock()
_last_hash: str = "GENESIS"   # cache of last entry's hash
_entry_counter: int = 0


def _audit_dir() -> Path:
    p = Path(settings.data_dir) / "audit"
    p.mkdir(parents=True, exist_ok=True)
    (p / "roots").mkdir(parents=True, exist_ok=True)
    return p


def _events_file() -> Path:
    return _audit_dir() / "events.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_audit() -> None:
    """Read existing audit log to recover the last hash + entry counter.
    Called once at server startup."""
    global _last_hash, _entry_counter
    p = _events_file()
    if not p.exists():
        _last_hash = "GENESIS"
        _entry_counter = 0
        log.info("Audit log: starting fresh (no existing events.jsonl)")
        return
    last_entry: Optional[dict] = None
    n = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last_entry = json.loads(line)
                n += 1
            except json.JSONDecodeError:
                log.warning("Audit log has malformed line; ignoring.")
    if last_entry:
        _last_hash = last_entry.get("thisHash", "GENESIS")
        _entry_counter = last_entry.get("entryNo", 0)
    log.info("Audit log: loaded %d existing entries; last hash = %s",
             n, _last_hash[:16] + "…")


def append_event(*, actor: str, action: str, resource: dict | None = None,
                 payload: dict | None = None) -> dict:
    """Append one event to the audit log. Returns the persisted entry."""
    global _last_hash, _entry_counter
    redacted_payload = _redact_payload(payload or {})
    payload_hash = sha256_hex(canonical_json(redacted_payload))
    entry_no_local: int
    with _LOCK:
        _entry_counter += 1
        entry_no_local = _entry_counter
        prev = _last_hash
        body = {
            "entryNo": entry_no_local,
            "ts": _now_iso(),
            "actor": actor[:64],
            "action": action[:64],
            "resource": resource or {},
            "payloadHash": payload_hash,
            "prevHash": prev,
        }
        this_hash = sha256_hex(canonical_json(body))
        body["thisHash"] = this_hash
        body["signature"] = sign_bytes(this_hash.encode())
        try:
            with open(_events_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error("Failed to write audit entry: %s", e)
        _last_hash = this_hash
    log.debug("audit.append actor=%s action=%s entry=%d", actor, action, entry_no_local)
    return body


def _redact_payload(payload: dict) -> dict:
    """Recursively redact string fields in the payload before hashing."""
    if not isinstance(payload, dict):
        return payload
    out: dict = {}
    for k, v in payload.items():
        if isinstance(v, str):
            out[k] = redact_for_logs(v)
        elif isinstance(v, dict):
            out[k] = _redact_payload(v)
        elif isinstance(v, list):
            out[k] = [_redact_payload(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_chain(limit: Optional[int] = None) -> dict:
    """Walk the audit log and verify every hash + signature.

    Returns {ok, entries_checked, broken_at, reason} or {ok: True, ...}
    """
    p = _events_file()
    if not p.exists():
        return {"ok": True, "entries_checked": 0, "note": "audit log empty"}
    expected_prev = "GENESIS"
    checked = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "broken_at": checked,
                        "reason": "JSON parse error"}
            checked += 1
            if e.get("prevHash") != expected_prev:
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "prevHash mismatch — entries reordered or deleted"}
            # Recompute the hash without `thisHash` and `signature` fields
            body = {k: v for k, v in e.items() if k not in ("thisHash", "signature")}
            computed = sha256_hex(canonical_json(body))
            if computed != e.get("thisHash"):
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "thisHash mismatch — entry was edited"}
            if not verify_signature(computed.encode(), e.get("signature", "")):
                return {"ok": False, "broken_at": e.get("entryNo"),
                        "reason": "signature invalid"}
            expected_prev = e["thisHash"]
            if limit and checked >= limit:
                break
    return {"ok": True, "entries_checked": checked,
            "last_hash": expected_prev[:32] + "…" if expected_prev != "GENESIS" else "GENESIS"}


# ---------------------------------------------------------------------------
# Pagination + filtering for the admin viewer
# ---------------------------------------------------------------------------

def query(*, actor: Optional[str] = None, action: Optional[str] = None,
          since: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Iterate the audit log and return entries matching the filters."""
    p = _events_file()
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if actor and actor not in e.get("actor", ""):
                continue
            if action and action not in e.get("action", ""):
                continue
            if since and e.get("ts", "") < since:
                continue
            out.append(e)
    # Most-recent first, capped at limit
    return list(reversed(out))[:limit]


def query_for_citizen(citizen_id: str, limit: int = 1000) -> list[dict]:
    """Return audit entries where actor OR resource references this citizen."""
    p = _events_file()
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("actor") == citizen_id:
                out.append(e)
                continue
            r = e.get("resource") or {}
            if r.get("citizenId") == citizen_id:
                out.append(e)
    return out[-limit:]


# ---------------------------------------------------------------------------
# Daily Merkle root
# ---------------------------------------------------------------------------

def compute_daily_root(d: Optional[date] = None) -> dict:
    """Compute Merkle root over all entries for a given day. Default = today."""
    target_date = (d or date.today()).isoformat()
    p = _events_file()
    if not p.exists():
        return {"date": target_date, "entries": 0, "root": sha256_hex(b"")}
    leaves: list[str] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", "").startswith(target_date):
                leaves.append(e["thisHash"])
    root = merkle_root(leaves)
    info = {
        "date": target_date, "entries": len(leaves), "root": root,
        "computed_at": _now_iso(),
    }
    root_path = _audit_dir() / "roots" / f"{target_date}.json"
    try:
        root_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not persist daily root: %s", e)
    return info


async def daily_root_loop() -> None:
    """Background task — compute the daily root every hour while the server runs."""
    await asyncio.sleep(5)
    while True:
        try:
            info = compute_daily_root()
            log.info("Daily Merkle root for %s computed (%d entries): %s…",
                     info["date"], info["entries"], info["root"][:16])
        except Exception as e:
            log.warning("Daily root computation failed: %s", e)
        await asyncio.sleep(3600)
