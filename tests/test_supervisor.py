"""Unit tests for backend.substrate.supervisor — routing, quality gate,
and escalation. No running server or LLM needed; quality_gate reads/writes
plain JSON via a tmp_path data_dir, exactly like judge.stats() does.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from backend.substrate import supervisor as sup
from backend.substrate.authn import Principal
from backend.substrate.schemas import Role


def _principal(role: Role, sub: str = "test-user") -> Principal:
    return Principal(sub=sub, name=sub, role=role)


# --- classify() --------------------------------------------------------

def test_classify_default_mentor_when_no_signal():
    d = sup.classify("What courses are available after class 10?", Role.learner)
    assert d.persona == sup.PERSONA_MENTOR
    assert not d.overridden


def test_classify_routes_contentqa_hint_for_sme():
    d = sup.classify("What's our QP coverage for HSS/Q5101?", Role.sme)
    assert d.persona == sup.PERSONA_CONTENTQA


def test_classify_does_not_route_contentqa_for_learner_even_with_hint():
    # A learner role isn't QP-capable — classify must not route them there
    # even though the question text matches the content-qa hint regex.
    d = sup.classify("What's our QP coverage for HSS/Q5101?", Role.learner)
    assert d.persona != sup.PERSONA_CONTENTQA
    assert d.persona == sup.PERSONA_MENTOR


def test_classify_routes_officer_action_for_officer():
    d = sup.classify("Please approve and file this on my behalf", Role.officer)
    assert d.persona == sup.PERSONA_OFFICER


def test_classify_never_routes_learner_to_officer_persona():
    # Role isolation is the safety-critical property here: a citizen must
    # never be routed into the officer-only persona no matter the phrasing.
    d = sup.classify("Please approve and file this on my behalf", Role.learner)
    assert d.persona != sup.PERSONA_OFFICER
    assert d.persona == sup.PERSONA_MENTOR


def test_classify_respects_requested_persona_when_capable():
    d = sup.classify("hello", Role.sme, requested_persona=sup.PERSONA_CONTENTQA)
    assert d.persona == sup.PERSONA_CONTENTQA
    assert not d.overridden


def test_classify_falls_back_when_requested_persona_not_capable():
    # A learner asking for content_qa (not in their capable set) must fall
    # back to mentor rather than honoring an out-of-scope request.
    d = sup.classify("hello", Role.learner, requested_persona=sup.PERSONA_CONTENTQA)
    assert d.persona == sup.PERSONA_MENTOR


# --- quality_gate() ------------------------------------------------------

def _write_judge_scores(data_dir: Path, scored: int, hallucinated: int) -> None:
    """Write a data/judge_scores.jsonl that backend.substrate.judge.stats()
    will read the same way it does in production."""
    path = data_dir / "judge_scores.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(scored):
            groundedness = 0.0 if i < hallucinated else 1.0
            f.write(json.dumps({
                "interaction_id": f"int_{i}",
                "groundedness": groundedness,
                "hallucinated": i < hallucinated,
            }) + "\n")


def test_quality_gate_flags_when_over_threshold(tmp_path, monkeypatch):
    # Fake out judge.stats() directly rather than depending on its exact
    # on-disk schema — keeps this test robust to judge.py internals changing.
    monkeypatch.setattr(
        "backend.substrate.judge.stats",
        lambda data_dir: {"scored": 10, "hallucination_rate_pct": 55.0},
    )
    flag = sup.quality_gate(data_dir=str(tmp_path), threshold_pct=20.0)
    assert flag is not None
    assert flag["level"] == "caution"
    assert flag["hallucination_rate_pct"] == 55.0


def test_quality_gate_silent_when_under_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.substrate.judge.stats",
        lambda data_dir: {"scored": 10, "hallucination_rate_pct": 5.0},
    )
    flag = sup.quality_gate(data_dir=str(tmp_path), threshold_pct=20.0)
    assert flag is None


def test_quality_gate_never_raises_on_stats_failure(monkeypatch, tmp_path):
    def _boom(data_dir):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("backend.substrate.judge.stats", _boom)
    # Must not raise — a stats-read failure must not block a citizen's answer.
    flag = sup.quality_gate(data_dir=str(tmp_path))
    assert flag is None


# --- escalation ------------------------------------------------------------

def test_maybe_escalate_on_refusal(tmp_path):
    principal = _principal(Role.learner)
    routing = sup.RoutingDecision(persona=sup.PERSONA_MENTOR, reason="test")
    result = sup.maybe_escalate(principal, "why no answer?", routing,
                                 refusal_reason="no_evidence", data_dir=str(tmp_path))
    assert result.escalated
    assert result.escalation_id is not None

    stored = sup.list_escalations(data_dir=str(tmp_path))
    assert len(stored) == 1
    assert stored[0]["refusal_reason"] == "no_evidence"
    assert stored[0]["status"] == "open"


def test_maybe_escalate_on_officer_routing_even_without_refusal(tmp_path):
    principal = _principal(Role.officer, sub="rajesh")
    routing = sup.RoutingDecision(persona=sup.PERSONA_OFFICER, reason="test")
    result = sup.maybe_escalate(principal, "approve this", routing,
                                 refusal_reason=None, data_dir=str(tmp_path))
    assert result.escalated


def test_no_escalation_for_ordinary_mentor_answer(tmp_path):
    principal = _principal(Role.learner)
    routing = sup.RoutingDecision(persona=sup.PERSONA_MENTOR, reason="test")
    result = sup.maybe_escalate(principal, "which course?", routing,
                                 refusal_reason=None, data_dir=str(tmp_path))
    assert not result.escalated
    assert sup.list_escalations(data_dir=str(tmp_path)) == []


def test_list_escalations_filters_by_status(tmp_path):
    principal = _principal(Role.learner)
    routing = sup.RoutingDecision(persona=sup.PERSONA_MENTOR, reason="test")
    sup.maybe_escalate(principal, "q1", routing, refusal_reason="no_evidence",
                        data_dir=str(tmp_path))
    sup.maybe_escalate(principal, "q2", routing, refusal_reason="unsafe",
                        data_dir=str(tmp_path))
    all_open = sup.list_escalations(data_dir=str(tmp_path), status="open")
    assert len(all_open) == 2
    none_closed = sup.list_escalations(data_dir=str(tmp_path), status="closed")
    assert none_closed == []


def test_quality_gate_suppressed_when_sample_too_small(monkeypatch, tmp_path):
    # Regression for the real situation found in this repo: 55.6% rate from
    # only 3 scored interactions must NOT raise a user-facing flag.
    monkeypatch.setattr(
        "backend.substrate.judge.stats",
        lambda data_dir: {"scored": 3, "hallucination_rate_pct": 55.6},
    )
    flag = sup.quality_gate(data_dir=str(tmp_path), threshold_pct=20.0, min_scored=10)
    assert flag is None


def test_quality_gate_fires_once_sample_is_sufficient(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.substrate.judge.stats",
        lambda data_dir: {"scored": 25, "hallucination_rate_pct": 55.6},
    )
    flag = sup.quality_gate(data_dir=str(tmp_path), threshold_pct=20.0, min_scored=10)
    assert flag is not None
    assert flag["scored"] == 25
