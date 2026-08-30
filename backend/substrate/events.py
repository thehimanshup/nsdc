"""Synthetic skilling event store + generator (ST-701, RFP 4.B.1f).

SQLite-backed (stdlib, zero-dependency) with a deliberately thin interface
so the delivery-phase swap to Postgres/ClickHouse is mechanical. Events
follow the canonical SkillingEvent schema; all learners are SYN- prefixed
(synthetic-only guard enforced by the schema).

The generator is DETERMINISTIC (seeded RNG) and plants known anomalies the
Officer Copilot demo can find:
  - TC-DEL-002 (South Delhi): attendance sags to ~62% in the final month
  - TC-DEL-003 (South West Delhi): elevated dropout (~25%)
  - TC-DEL-004: strong performer (control)
"""
from __future__ import annotations

import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schemas import SkillingEvent

DB_NAME = "skilling_events.db"

CENTRES = [
    # (centre_id, district, state, course_id, qp_code, cohort, attend_base, dropout)
    ("TC-DEL-001", "Central Delhi", "Delhi", "crs-gda-01", "HSS/Q5101", 40, 0.88, 0.10),
    ("TC-DEL-002", "South Delhi", "Delhi", "crs-gda-01", "HSS/Q5101", 45, 0.85, 0.12),
    ("TC-DEL-003", "South West Delhi", "Delhi", "crs-hha-01", "HSS/Q5102", 35, 0.80, 0.25),
    ("TC-DEL-004", "North West Delhi", "Delhi", "crs-phleb-01", "HSS/Q0301", 30, 0.92, 0.06),
]
SCHEME = "pmkvy4"
TRAINING_DAYS = 66          # ~3 months, 5-day weeks
ANOMALY_CENTRE = "TC-DEL-002"
ANOMALY_FROM_DAY = 44       # last month attendance sag


def _connect(data_dir: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(data_dir) / DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        ts TEXT NOT NULL,
        learner_id TEXT NOT NULL CHECK (learner_id LIKE 'SYN-%'),
        centre_id TEXT NOT NULL,
        district TEXT NOT NULL,
        state TEXT NOT NULL,
        scheme_id TEXT NOT NULL,
        course_id TEXT NOT NULL,
        qp_code TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        consent_token_id TEXT DEFAULT '',
        source_system TEXT NOT NULL DEFAULT 'synthetic-gen-v1',
        schema_version TEXT NOT NULL DEFAULT '0.1'
    );
    CREATE INDEX IF NOT EXISTS idx_events_centre ON events(centre_id, event_type);
    CREATE INDEX IF NOT EXISTS idx_events_district ON events(district);
    """)
    conn.commit()


def generate(data_dir: str | Path = "data", seed: int = 20260719,
             start: str = "2026-04-06") -> dict:
    rng = random.Random(seed)
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    conn = _connect(data_dir)
    init_db(conn)
    conn.execute("DELETE FROM events")
    counts: dict[str, int] = {}
    ln = 0

    def emit(ev_type, ts, learner, c, payload=None):
        nonlocal counts
        ev = SkillingEvent(
            event_id=str(uuid.uuid4()), event_type=ev_type, ts=ts,
            learner_id=learner, centre_id=c[0], district=c[1], state=c[2],
            scheme_id=SCHEME, course_id=c[3], qp_code=c[4],
            payload=payload or {},
            consent_token_id=f"ct-syn-{learner.lower()}")
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.event_type.value, ev.ts.isoformat(), ev.learner_id,
             ev.centre_id, ev.district, ev.state, ev.scheme_id, ev.course_id,
             ev.qp_code, json.dumps(ev.payload), ev.consent_token_id,
             ev.source_system, ev.schema_version))
        counts[ev_type] = counts.get(ev_type, 0) + 1

    for c in CENTRES:
        centre_id, _, _, _, _, cohort, attend_base, dropout_rate = c
        for i in range(cohort):
            ln += 1
            learner = f"SYN-{ln:06d}"
            emit("enrolment", t0, learner, c,
                 {"age": rng.randint(17, 32), "education": "class10"})
            drops_out = rng.random() < dropout_rate
            drop_day = rng.randint(15, 55) if drops_out else None
            day = t0
            training_day = 0
            while training_day < TRAINING_DAYS:
                day += timedelta(days=1)
                if day.weekday() >= 5:
                    continue
                training_day += 1
                if drop_day and training_day >= drop_day:
                    break
                p = attend_base
                if centre_id == ANOMALY_CENTRE and training_day >= ANOMALY_FROM_DAY:
                    p = 0.62
                emit("attendance", day, learner, c,
                     {"present": rng.random() < p, "training_day": training_day})
            if not drops_out:
                a_day = day + timedelta(days=7)
                score = round(rng.uniform(55, 95), 1)
                emit("assessment", a_day, learner, c,
                     {"score": score, "max_score": 100, "mode": "theory+practical"})
                if score >= 60:
                    emit("certification", a_day + timedelta(days=14), learner, c,
                         {"certificate_id": f"SYN-CERT-{ln:06d}"})
                    if rng.random() < 0.55:
                        emit("placement", a_day + timedelta(days=45), learner, c,
                             {"sector": "healthcare",
                              "monthly_salary_band": rng.choice(
                                  ["10-13k", "13-16k", "16-20k"])})
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    return {"total_events": total, "by_type": counts, "seed": seed,
            "centres": [c[0] for c in CENTRES]}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=1))
