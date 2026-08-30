"""Tests: groundedness judge (ST-504) + superlative gate (G-026 fix)."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.substrate.gates import EvidenceBundle, superlative_gate
from backend.substrate.judge import judge_contract, stats
from backend.substrate.schemas import (ChunkPayload, CitationContract, Claim,
                                       Purpose, Role)


def _chunk(cid="d1#1", doc="d1", text="GDA is NSQF level 4. Duration 390 hours."):
    return ChunkPayload(chunk_id=cid, doc_id=doc, text=text,
                        chunk_hash=f"sha256:{cid}", allowed_roles=[Role.learner],
                        allowed_purposes=[Purpose.course_guidance])


def _contract(cited=True):
    return CitationContract(
        answer_markdown="GDA is NSQF level 4.",
        claims=[Claim(text="GDA is NSQF level 4.",
                      citation_ids=["d1#1"] if cited else [])])


class FakeLLM:
    mock_mode = False
    def __init__(self, reply): self.reply = reply
    async def chat_complete(self, **kw): return self.reply


# --- judge -------------------------------------------------------------------

def test_judge_skips_in_mock_mode(tmp_path, monkeypatch):
    import backend.llm as llm_mod
    class MockLLM: mock_mode = True
    monkeypatch.setattr(llm_mod, "get_llm_for", lambda p=None: MockLLM())
    rec = asyncio.run(judge_contract("int_1", _contract(), {"d1#1": "text"}, tmp_path))
    assert rec["status"] == "skipped"
    s = stats(tmp_path)
    assert s["scored"] == 0 and s["skipped"] == 1
    assert "hallucination_rate_pct" not in s   # never fabricated


def test_judge_scores_with_live_like_llm(tmp_path, monkeypatch):
    import backend.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_for",
                        lambda p=None: FakeLLM('[{"claim":1,"verdict":"supported"}]'))
    rec = asyncio.run(judge_contract("int_2", _contract(),
                                     {"d1#1": "GDA is NSQF level 4."}, tmp_path))
    assert rec["status"] == "scored" and rec["groundedness"] == 1.0
    assert rec["unsupported_claims"] == 0
    s = stats(tmp_path)
    assert s["mean_groundedness"] == 1.0 and s["hallucination_rate_pct"] == 0.0


def test_judge_flags_unsupported(tmp_path, monkeypatch):
    import backend.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_for",
                        lambda p=None: FakeLLM('[{"claim":1,"verdict":"unsupported"}]'))
    rec = asyncio.run(judge_contract("int_3", _contract(), {"d1#1": "unrelated"}, tmp_path))
    assert rec["groundedness"] == 0.0 and rec["unsupported_claims"] == 1


def test_judge_error_never_raises(tmp_path, monkeypatch):
    import backend.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_for",
                        lambda p=None: FakeLLM("not json at all"))
    rec = asyncio.run(judge_contract("int_4", _contract(), {}, tmp_path))
    assert rec["status"] == "error"        # recorded, not raised


def test_judge_ignores_refusals(tmp_path):
    from backend.substrate.gates import no_evidence_contract
    rec = asyncio.run(judge_contract("int_5", no_evidence_contract(), {}, tmp_path))
    assert rec is None


# --- superlative gate -----------------------------------------------------------

def test_superlative_refused_without_comparative_evidence():
    b = EvidenceBundle(chunks=[(_chunk(), 0.7)])
    d = superlative_gate("Which centre has the best placement record?", b)
    assert not d.allowed


def test_superlative_allowed_with_comparative_numeric_evidence():
    b = EvidenceBundle(chunks=[
        (_chunk("a#1", "doc-a", "Centre A placement 55% of certified."), 0.7),
        (_chunk("b#1", "doc-b", "Centre B placement 62% of certified."), 0.6)])
    d = superlative_gate("Which centre has the best placement record?", b)
    assert d.allowed


def test_non_superlative_unaffected():
    b = EvidenceBundle(chunks=[(_chunk(), 0.7)])
    assert superlative_gate("What is the NSQF level of GDA?", b).allowed
