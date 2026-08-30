"""Scheme catalog + eligibility engine — Phase 6e.

Loads data/schemes.json (admin-editable, seeded from the shipped file in the
package data dir). Provides:
  - search(query, state, family)    — life-situation / keyword discovery
  - check_eligibility(scheme, profile) — explainable rule evaluation
  - families()                      — the six scheme families

Eligibility is intentionally explainable: each rule returns pass/fail with a
human label, so an agent can say *why* a citizen does or doesn't qualify
rather than emitting a black-box yes/no.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("schemes")

FAMILIES = ["skilling", "apprenticeship", "entrepreneurship"]

FAMILY_LABELS = {
    "skilling": "Skill training schemes",
    "apprenticeship": "Apprenticeship schemes",
    "entrepreneurship": "Entrepreneurship & artisan schemes",
}

_SCHEMES: dict[str, dict] = {}


def _data_path() -> Path:
    p = Path(settings.data_dir) / "schemes.json"
    if not p.exists():
        # fall back to the shipped copy in the package data dir
        alt = Path(__file__).resolve().parent.parent / "data" / "schemes.json"
        if alt.exists():
            return alt
    return p


def load() -> int:
    global _SCHEMES
    p = _data_path()
    try:
        _SCHEMES = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("schemes load failed (%s): %s", p, e)
        _SCHEMES = {}
    log.info("Loaded %d schemes across %d families", len(_SCHEMES), len(FAMILIES))
    return len(_SCHEMES)


def get(scheme_id: str) -> Optional[dict]:
    return _SCHEMES.get(scheme_id)


def all_schemes() -> list[dict]:
    return list(_SCHEMES.values())


def _applies_to_state(scheme: dict, state_code: str) -> bool:
    states = scheme.get("states", ["ALL"])
    return "ALL" in states or not state_code or state_code.upper() in states


# Keyword → which families/life-situations they hint at. Used for fuzzy search.
_HINTS = {
    "housing": ["house", "home", "awas", "pucca", "shelter", "rent", "slum"],
    "women_welfare": ["woman", "women", "widow", "maternity", "pregnant",
                      "mother", "girl", "magalir", "matru", "kanya"],
    "child_welfare": ["child", "children", "kid", "anganwadi", "nutrition",
                      "school", "scholarship", "student", "baby", "infant"],
    "senior_citizen": ["old", "senior", "elderly", "pension", "aged",
                       "disability", "disabled", "vridha"],
    "farmer": ["farmer", "farm", "crop", "kisan", "agriculture", "land",
               "soil", "harvest", "cultivat"],
    "health": ["health", "hospital", "treatment", "insurance", "medical",
               "ayushman", "surgery", "illness", "disease", "dialysis"],
}


def search(query: str = "", *, state_code: str = "", family: str = "",
           limit: int = 10) -> list[dict]:
    """Find schemes by keyword / life-situation, scoped to the citizen's state."""
    q = (query or "").lower()
    results: list[tuple[int, dict]] = []
    for s in _SCHEMES.values():
        if not _applies_to_state(s, state_code):
            continue
        if family and s.get("family") != family:
            continue
        score = 0
        text = f"{s.get('name','')} {s.get('summary','')} {s.get('benefit','')}".lower()
        if q:
            if q in text:
                score += 5
            for tok in q.split():
                if len(tok) > 2 and tok in text:
                    score += 2
            # life-situation hints → family match
            fam = s.get("family", "")
            for kw in _HINTS.get(fam, []):
                if kw in q:
                    score += 3
        else:
            score = 1
        # prefer state-specific schemes when a state is known
        if state_code and state_code.upper() in s.get("states", []):
            score += 1
        if score > 0:
            results.append((score, s))
    results.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in results[:limit]]


def _cmp(op: str, actual, expected) -> bool:
    try:
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == "in":
            return actual in expected
        if op == "<=":
            return actual is not None and float(actual) <= float(expected)
        if op == ">=":
            return actual is not None and float(actual) >= float(expected)
        if op == "<":
            return actual is not None and float(actual) < float(expected)
        if op == ">":
            return actual is not None and float(actual) > float(expected)
    except Exception:
        return False
    return False


def check_eligibility(scheme_id: str, profile: dict) -> dict:
    """Evaluate a scheme's rules against a (possibly partial) citizen profile.

    Returns an explainable verdict:
      {eligible, status: eligible|not_eligible|need_more_info, checks: [...]}
    Unknown fields → 'unknown' (need_more_info) rather than a hard fail.
    """
    s = _SCHEMES.get(scheme_id)
    if not s:
        return {"error": "unknown_scheme"}
    checks = []
    any_fail = False
    any_unknown = False
    for rule in s.get("eligibility_rules", []):
        field = rule.get("field")
        op = rule.get("op", "==")
        expected = rule.get("value")
        label = rule.get("label", field)
        if field not in profile or profile.get(field) in (None, ""):
            checks.append({"label": label, "result": "unknown",
                           "field": field, "needed": expected})
            any_unknown = True
            continue
        ok = _cmp(op, profile.get(field), expected)
        checks.append({"label": label, "result": "pass" if ok else "fail",
                       "field": field})
        if not ok:
            any_fail = True
    if any_fail:
        status = "not_eligible"
    elif any_unknown:
        status = "need_more_info"
    else:
        status = "eligible"
    return {
        "scheme_id": scheme_id,
        "scheme_name": s.get("name"),
        "eligible": status == "eligible",
        "status": status,
        "checks": checks,
        "documents_required": s.get("documents_required", []),
        "benefit": s.get("benefit"),
        "helpline": s.get("helpline"),
        "official_url": s.get("official_url"),
        # Phase 6e — never present an eligibility result as a guarantee.
        "disclaimer": ("Indicative only, based on the details provided. Final "
                       "eligibility is decided by the department after official "
                       "verification of your documents."),
    }


def families_json() -> list[dict]:
    out = []
    for fam in FAMILIES:
        members = [s for s in _SCHEMES.values() if s.get("family") == fam]
        out.append({"family": fam, "label": FAMILY_LABELS[fam],
                    "count": len(members),
                    "schemeIds": [s["scheme_id"] for s in members]})
    return out
