"""Tests for the judge's verdict parsing — regression for the 4 real
'judge returned no parseable verdicts' error records found in
data/judge_scores.jsonl, caused by the previous single-regex parse."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.judge import _parse_verdicts


def test_plain_array():
    raw = '[{"claim": 1, "verdict": "supported"}, {"claim": 2, "verdict": "unsupported"}]'
    v = _parse_verdicts(raw)
    assert len(v) == 2 and v[0]["verdict"] == "supported"


def test_markdown_fenced_array():
    raw = '```json\n[{"claim": 1, "verdict": "partial"}]\n```'
    v = _parse_verdicts(raw)
    assert len(v) == 1 and v[0]["verdict"] == "partial"


def test_wrapper_object_with_verdicts_key():
    raw = '{"verdicts": [{"claim": 1, "verdict": "supported"}]}'
    v = _parse_verdicts(raw)
    assert len(v) == 1


def test_single_bare_object_for_one_claim():
    raw = '{"claim": 1, "verdict": "unsupported"}'
    v = _parse_verdicts(raw)
    assert len(v) == 1 and v[0]["verdict"] == "unsupported"


def test_array_with_trailing_prose():
    raw = 'Here are my verdicts:\n[{"claim": 1, "verdict": "supported"}]\nLet me know if you need more.'
    v = _parse_verdicts(raw)
    assert len(v) == 1


def test_garbage_returns_empty_not_crash():
    assert _parse_verdicts("I cannot evaluate these claims.") == []
    assert _parse_verdicts("") == []
    assert _parse_verdicts("[not json at all") == []


# --- verdict-value validation (root-cause fix for the phantom 55.6% rate) ---

def test_template_echo_detected_as_invalid():
    # The exact failure found in data/judge_scores.jsonl: the model echoed
    # the prompt's placeholder instead of choosing a verdict.
    v = _parse_verdicts('[{"claim": 1, "verdict": "supported|partial|unsupported"}]')
    assert len(v) == 1
    assert v[0]["verdict"] not in ("supported", "partial", "unsupported")


def test_judge_system_prompt_contains_no_echoable_pipe_placeholder():
    from backend.substrate.judge import JUDGE_SYSTEM
    assert "supported|partial|unsupported" not in JUDGE_SYSTEM
    # and it now shows a concrete, valid example instead
    assert '"verdict": "supported"' in JUDGE_SYSTEM
