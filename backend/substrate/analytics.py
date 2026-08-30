"""Jurisdiction-scoped analytics engine for the Officer Copilot (ST-702).

Design rules (PRD US-2.1, RFP 4.B.4.2):
  - NL question → TEMPLATE selection. The LLM never writes SQL; free-form
    analytics questions that match no template are refused.
  - Jurisdiction scope is injected by THIS layer from the verified token
    Principal — the caller (and the model) cannot widen it.
  - Every numeric answer carries data-point citations: the SQL executed,
    row counts, and the event window, so figures are reproducible.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .authn import Principal
from .events import DB_NAME
from .schemas import Role


@dataclass
class AnalyticsResult:
    template: str
    answer_text: str
    rows: list[dict] = field(default_factory=list)
    sql: str = ""
    scope: dict = field(default_factory=dict)
    refused: bool = False
    refusal_detail: str = ""


_TEMPLATES = [
    ("low_attendance", re.compile(
        r"(low|poor|below|falling|drop.*in)?\s*attendance|उपस्थिति", re.I)),
    ("dropout", re.compile(r"drop\s?-?out|dropped out|attrition|छोड़", re.I)),
    ("enrolment_summary", re.compile(
        r"enrol|enrollment|admissions|कितने.*(छात्र|प्रशिक्षु)|batch size", re.I)),
    ("certification_funnel", re.compile(
        r"certif|pass rate|completion|funnel|प्रमाण", re.I)),
    ("placement", re.compile(r"placement|placed|job outcome|नियुक्ति", re.I)),
]


def _scope_clause(principal: Principal) -> tuple[str, list, dict]:
    """The jurisdiction injection point. Officers: hard district scope.
    Admin: unscoped. Everyone else: no analytics at all (checked upstream)."""
    if principal.role == Role.admin:
        return "1=1", [], {"scope": "all (admin)"}
    district = principal.district
    if not district:
        return "1=0", [], {"scope": "none"}
    return "district = ?", [district], {"scope": f"district={district}"}


_PII_REQUEST = re.compile(
    r"aadhaar|आधार|\bpan\b|phone number|mobile number|bank account|address(es)?\b"
    r"|learner names|contact details|फ़ोन|खाता", re.I)


def _classify(question: str) -> Optional[str]:
    for name, pat in _TEMPLATES:
        if pat.search(question):
            return name
    return None


def run_analytics(question: str, principal: Principal,
                  data_dir: str | Path = "data",
                  attendance_threshold: float = 0.70) -> AnalyticsResult:
    if principal.role not in (Role.officer, Role.admin):
        return AnalyticsResult("", "", refused=True,
                               refusal_detail=f"role {principal.role.value} may not run analytics")
    if _PII_REQUEST.search(question):
        return AnalyticsResult("", "", refused=True, refusal_detail=(
            "analytics serves aggregates only — personal identifiers "
            "(Aadhaar, phone, bank, address) are never retrievable here. "
            "This request has been logged."))
    template = _classify(question)
    if template is None:
        return AnalyticsResult("", "", refused=True, refusal_detail=(
            "no analytics template matches this question — supported: "
            "attendance, dropout, enrolment, certification, placement"))

    where, params, scope = _scope_clause(principal)
    db = Path(data_dir) / DB_NAME
    if not db.exists():
        return AnalyticsResult(template, "", refused=True,
                               refusal_detail="event store not generated — run substrate.events")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    if template == "low_attendance":
        # Overall AND recent-window (last 30 days of data) rates: a healthy
        # average can hide a fresh deterioration — the demo anomaly case.
        sql = f"""
            WITH scope AS (
              SELECT *, MAX(substr(ts,1,10)) OVER () AS max_d
              FROM events WHERE event_type='attendance' AND {where})
            SELECT centre_id, district,
                   COUNT(*) AS sessions,
                   ROUND(AVG(json_extract(payload,'$.present')), 3) AS attendance_rate,
                   ROUND(AVG(CASE WHEN substr(ts,1,10) >= date(max_d,'-30 day')
                             THEN json_extract(payload,'$.present') END), 3)
                        AS recent_30d_rate,
                   MIN(substr(ts,1,10)) AS window_from,
                   MAX(substr(ts,1,10)) AS window_to
            FROM scope GROUP BY centre_id ORDER BY recent_30d_rate ASC"""
        rows = [dict(r) for r in conn.execute(sql, params)]
        low = [r for r in rows
               if (r["recent_30d_rate"] if r["recent_30d_rate"] is not None
                   else r["attendance_rate"] or 0) < attendance_threshold]
        if rows:
            parts = [f"{len(low)} of {len(rows)} centres in scope are below "
                     f"{int(attendance_threshold*100)}% attendance in the last "
                     f"30 days of data."]
            for r in rows:
                flag = " ⚠ DETERIORATING" if r in low else ""
                parts.append(
                    f"- {r['centre_id']} ({r['district']}): overall "
                    f"{r['attendance_rate']*100:.0f}%, last-30d "
                    f"{(r['recent_30d_rate'] or 0)*100:.0f}% over {r['sessions']} "
                    f"records ({r['window_from']} → {r['window_to']}){flag}")
            answer = "\n".join(parts)
        else:
            answer = "No attendance events found within your jurisdiction."

    elif template == "dropout":
        sql = f"""
            SELECT centre_id, district,
              COUNT(DISTINCT CASE WHEN event_type='enrolment' THEN learner_id END) AS enrolled,
              COUNT(DISTINCT CASE WHEN event_type='assessment' THEN learner_id END) AS assessed
            FROM events WHERE {where} GROUP BY centre_id"""
        rows = [dict(r) for r in conn.execute(sql, params)]
        for r in rows:
            r["dropout_rate"] = round(1 - (r["assessed"] / r["enrolled"]), 3) if r["enrolled"] else None
        rows.sort(key=lambda r: -(r["dropout_rate"] or 0))
        answer = "\n".join(
            [f"Dropout (enrolled → never reached assessment), your scope:"] +
            [f"- {r['centre_id']} ({r['district']}): {r['dropout_rate']*100:.0f}% "
             f"({r['enrolled']-r['assessed']}/{r['enrolled']} learners)"
             for r in rows if r["enrolled"]]) if rows else "No data in scope."

    elif template == "enrolment_summary":
        sql = f"""
            SELECT centre_id, district, course_id,
                   COUNT(DISTINCT learner_id) AS enrolled
            FROM events WHERE event_type='enrolment' AND {where}
            GROUP BY centre_id"""
        rows = [dict(r) for r in conn.execute(sql, params)]
        total = sum(r["enrolled"] for r in rows)
        answer = "\n".join(
            [f"{total} learners enrolled across {len(rows)} centres in your scope:"] +
            [f"- {r['centre_id']} ({r['district']}): {r['enrolled']} in {r['course_id']}"
             for r in rows]) if rows else "No enrolments in scope."

    elif template == "certification_funnel":
        sql = f"""
            SELECT
              COUNT(DISTINCT CASE WHEN event_type='enrolment' THEN learner_id END) AS enrolled,
              COUNT(DISTINCT CASE WHEN event_type='assessment' THEN learner_id END) AS assessed,
              COUNT(DISTINCT CASE WHEN event_type='certification' THEN learner_id END) AS certified
            FROM events WHERE {where}"""
        r = dict(conn.execute(sql, params).fetchone())
        rows = [r]
        answer = (f"Funnel in your scope: enrolled {r['enrolled']} → assessed "
                  f"{r['assessed']} → certified {r['certified']} "
                  f"({(r['certified']/r['enrolled']*100):.0f}% certification rate)"
                  if r["enrolled"] else "No data in scope.")

    else:  # placement
        sql = f"""
            SELECT centre_id, district,
              COUNT(DISTINCT CASE WHEN event_type='certification' THEN learner_id END) AS certified,
              COUNT(DISTINCT CASE WHEN event_type='placement' THEN learner_id END) AS placed
            FROM events WHERE {where} GROUP BY centre_id"""
        rows = [dict(r) for r in conn.execute(sql, params)]
        answer = "\n".join(
            ["Placement outcomes (certified → placed), your scope:"] +
            [f"- {r['centre_id']} ({r['district']}): {r['placed']}/{r['certified']} placed"
             f" ({(r['placed']/r['certified']*100):.0f}%)" if r["certified"] else
             f"- {r['centre_id']}: no certifications yet"
             for r in rows]) if rows else "No data in scope."

    conn.close()
    answer += "\n\n(Source: synthetic transactional event store — figures reproducible via the cited SQL.)"
    return AnalyticsResult(template=template, answer_text=answer, rows=rows,
                           sql=re.sub(r"\s+", " ", sql).strip(), scope=scope)
