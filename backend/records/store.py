"""Records store — JSON-backed, atomic write, in-memory indexes.

Mirrors backend/store.py: dead-simple file persistence now, swap to Postgres
in Phase 7a without changing the interface. The records table is expected to
be the highest-write table in the system, so the (citizen, status) indexes
live in memory and are rebuilt on load.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import settings
from .models import Record, mint_record_id, OPEN_STATUSES

log = logging.getLogger("records.store")
_LOCK = threading.RLock()


def msisdn_hash(msisdn: str) -> str:
    """Stable, non-reversible hash for public 'track by reference + mobile'
    lookup. We never store the raw number on the record (DPDP minimisation)."""
    m = (msisdn or "").strip().lstrip("+").lstrip("91")[-10:]
    return hashlib.sha256(("rec:" + m).encode("utf-8")).hexdigest()[:16]


class RecordsStore:
    def __init__(self) -> None:
        self.records: dict[str, Record] = {}      # record_id -> Record
        self._by_citizen: dict[str, list[str]] = {}
        self._seq: int = 0
        self._load()

    # -----------------------------------------------------------------
    def _path(self) -> str:
        os.makedirs(settings.data_dir, exist_ok=True)
        return os.path.join(settings.data_dir, "records.json")

    def _persist(self) -> None:
        try:
            payload = {
                "seq": self._seq,
                "records": {rid: r.to_dict() for rid, r in self.records.items()},
            }
            path = self._path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("records persist failed: %s", e)

    def _load(self) -> None:
        path = self._path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            self._seq = int(payload.get("seq", 0))
            for rid, d in payload.get("records", {}).items():
                self.records[rid] = Record.from_dict(d)
            self._reindex()
            log.info("Loaded %d records (seq=%d)", len(self.records), self._seq)
        except Exception as e:
            log.warning("records load failed: %s", e)

    def _reindex(self) -> None:
        self._by_citizen.clear()
        for r in self.records.values():
            self._by_citizen.setdefault(r.citizen_id, []).append(r.record_id)

    # -----------------------------------------------------------------
    def next_seq(self) -> int:
        with _LOCK:
            self._seq += 1
            return self._seq

    def new_id(self, kind: str, state_code: str) -> str:
        return mint_record_id(kind, state_code, self.next_seq())

    def add(self, record: Record) -> Record:
        with _LOCK:
            self.records[record.record_id] = record
            self._by_citizen.setdefault(record.citizen_id, []).append(record.record_id)
            self._persist()
        return record

    def save(self, record: Record) -> Record:
        with _LOCK:
            self.records[record.record_id] = record
            self._persist()
        return record

    def get(self, record_id: str) -> Optional[Record]:
        return self.records.get((record_id or "").strip().upper()) \
            or self.records.get(record_id)

    def for_citizen(self, citizen_id: str, *, kind: str = "", open_only: bool = False) -> list[Record]:
        out = [self.records[rid] for rid in self._by_citizen.get(citizen_id, [])
               if rid in self.records]
        if kind:
            out = [r for r in out if r.kind == kind]
        if open_only:
            out = [r for r in out if r.status in OPEN_STATUSES]
        out.sort(key=lambda r: r.created_at or "", reverse=True)
        return out

    def all(self) -> list[Record]:
        return list(self.records.values())

    def query(self, *, department_id: str = "", status: str = "",
              state_code: str = "", level: int = 0) -> list[Record]:
        out = list(self.records.values())
        if department_id:
            out = [r for r in out if r.department_id == department_id]
        if status:
            out = [r for r in out if r.status == status]
        if state_code:
            out = [r for r in out if r.state_code == state_code]
        if level:
            out = [r for r in out if r.current_level == level]
        out.sort(key=lambda r: r.updated_at or r.created_at or "", reverse=True)
        return out

    def open_sla_records(self) -> list[Record]:
        """Records whose SLA clock is live (for the escalation sweeper)."""
        from .models import SLA_TRACKED_STATUSES
        return [r for r in self.records.values()
                if r.status in SLA_TRACKED_STATUSES and r.sla_due_at]

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        by_dept: dict[str, int] = {}
        breached = 0
        for r in self.records.values():
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_dept[r.department_id] = by_dept.get(r.department_id, 0) + 1
            if r.sla_breached:
                breached += 1
        return {"total": len(self.records), "byStatus": by_status,
                "byDepartment": by_dept, "slaBreached": breached}


records_store = RecordsStore()
