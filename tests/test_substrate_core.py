"""Unit tests — substrate schemas, manifests, gates (no services needed)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from backend.substrate.gates import (EvidenceBundle, blocked_output_contract,
                                     enforce_citations, evidence_gate,
                                     no_evidence_contract)
from backend.substrate.manifest import (ChunkingConfig, IndexManifest,
                                        ManifestRegistry)
from backend.substrate.schemas import (ChunkPayload, CitationContract, Claim,
                                       ConsentToken, DocumentMeta, GoldEvalItem,
                                       Purpose, RefusalReason, Role,
                                       SkillingEvent)


def _chunk(**over):
    base = dict(chunk_id="d1#s1-c1", doc_id="d1", text="NSQF level 4 QP",
                chunk_hash="sha256:aa", allowed_roles=[Role.learner],
                allowed_purposes=[Purpose.course_guidance])
    base.update(over)
    return ChunkPayload(**base)


# --- schemas ---------------------------------------------------------------

def test_document_meta_fails_closed_on_empty_roles():
    with pytest.raises(Exception):
        DocumentMeta(doc_id="x-1", title="t", source_org="o",
                     doc_type="qp", allowed_roles=[])


def test_skilling_event_rejects_non_synthetic_learner():
    with pytest.raises(Exception):
        SkillingEvent(event_id="e1", event_type="enrolment",
                      ts=datetime.now(timezone.utc), learner_id="REAL-123",
                      centre_id="TC", district="d", state="s",
                      scheme_id="pmkvy4", course_id="c", qp_code="q")


def test_consent_token_validity_and_revocation():
    now = datetime.now(timezone.utc)
    tok = ConsentToken(consent_token_id="ct1", user_id="u1",
                       purpose=Purpose.course_guidance,
                       issued_at=now, expires_at=now + timedelta(hours=24))
    assert tok.valid_for(Purpose.course_guidance)
    assert not tok.valid_for(Purpose.scheme_admin)
    tok.revoked = True
    assert not tok.valid_for(Purpose.course_guidance)


# --- manifest ---------------------------------------------------------------

def test_manifest_deterministic(tmp_path):
    cfg = ChunkingConfig()
    m1 = IndexManifest(embedding_model="bge-m3", embedding_dim=1024,
                       chunking_config=cfg).finalise(["h2", "h1"])
    m2 = IndexManifest(embedding_model="bge-m3", embedding_dim=1024,
                       chunking_config=cfg).finalise(["h1", "h2"])  # order-insensitive
    assert m1.manifest_id == m2.manifest_id
    m3 = IndexManifest(embedding_model="OTHER", embedding_dim=1024,
                       chunking_config=cfg).finalise(["h1", "h2"])
    assert m3.manifest_id != m1.manifest_id
    reg = ManifestRegistry(tmp_path)
    reg.save(m1)
    assert reg.current_id() == m1.manifest_id
    assert reg.load(m1.manifest_id).corpus_snapshot_hash == m1.corpus_snapshot_hash


# --- evidence gate -----------------------------------------------------------

def test_evidence_gate_refuses_on_weak_evidence():
    weak = EvidenceBundle(chunks=[(_chunk(), 0.10)])
    d = evidence_gate(weak)
    assert not d.allowed and d.refusal_reason == RefusalReason.no_evidence


def test_evidence_gate_allows_strong_evidence():
    strong = EvidenceBundle(chunks=[(_chunk(), 0.72), (_chunk(chunk_id="c2"), 0.4)])
    assert evidence_gate(strong).allowed


def test_no_evidence_contract_bilingual():
    en = no_evidence_contract("en", nearest_alternative="Home Health Aide")
    hi = no_evidence_contract("hi")
    assert en.is_refusal and "Home Health Aide" in en.answer_markdown
    assert hi.is_refusal and hi.language == "hi"


# --- citation hard gate --------------------------------------------------------

def test_citation_gate_blocks_uncited_claims():
    c = CitationContract(answer_markdown="GDA is NSQF level 4.",
                         claims=[Claim(text="GDA is NSQF level 4.")])
    d = enforce_citations(c)
    assert not d.allowed and d.refusal_reason == RefusalReason.malformed_output


def test_citation_gate_passes_cited_claims():
    c = CitationContract(
        answer_markdown="GDA is NSQF level 4.",
        claims=[Claim(text="GDA is NSQF level 4.",
                      citation_ids=["hssc-qp-5101#s2-c1"])])
    assert enforce_citations(c).allowed


def test_citation_gate_fails_closed_on_factual_prose_without_claims():
    c = CitationContract(answer_markdown="The QP requires NSQF level 4.", claims=[])
    assert not enforce_citations(c).allowed


def test_citation_gate_allows_smalltalk_and_refusals():
    assert enforce_citations(CitationContract(answer_markdown="Hello! How can I help?")).allowed
    assert enforce_citations(no_evidence_contract()).allowed
    assert blocked_output_contract().is_refusal


# --- gold eval item -----------------------------------------------------------

def test_gold_item_roundtrip():
    it = GoldEvalItem(eval_id="G-001", lang="en", category="factual",
                      persona=Role.learner, query="q", expected_behavior="answer",
                      must_cite_docs=["hssc-qp-5101"])
    assert GoldEvalItem.model_validate_json(it.model_dump_json()) == it
