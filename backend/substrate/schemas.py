"""Governed data contracts for the Sovereign AI Substrate PoC.

These pydantic models are the single source of truth for:
  - document/chunk metadata          (PRD §8.1/§8.3, RFP 4.B.1b/c)
  - the structured citation contract (PRD FR-7, RFP KPI 7.2.4)
  - the canonical skilling event     (PRD §8.4, RFP 4.B.1f)
  - consent tokens                   (PRD §8.5, RFP 4.B.1e / DPDP)
  - gold eval items                  (PRD §8.6, RFP 4.B.3 / KPI 7.2.x)

Design rule: ingestion and generation FAIL CLOSED — records that do not
validate are rejected, answers that do not validate are refused.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "0.1"


# ---------------------------------------------------------------------------
# Roles / purposes / sensitivity — the RBAC vocabulary
# ---------------------------------------------------------------------------

class Role(str, Enum):
    learner = "learner"
    officer = "officer"
    sme = "sme"
    admin = "admin"


class Purpose(str, Enum):
    course_guidance = "course_guidance"
    scheme_admin = "scheme_admin"
    content_qa = "content_qa"
    evaluation = "evaluation"


class Sensitivity(str, Enum):
    public = "public"
    internal = "internal"
    restricted = "restricted"


# Role clearance ordering for sensitivity filtering (retrieval pre-filter).
SENSITIVITY_CLEARANCE: dict[Role, set[Sensitivity]] = {
    Role.learner: {Sensitivity.public},
    Role.sme: {Sensitivity.public, Sensitivity.internal},
    Role.officer: {Sensitivity.public, Sensitivity.internal},
    Role.admin: {Sensitivity.public, Sensitivity.internal, Sensitivity.restricted},
}


# ---------------------------------------------------------------------------
# Document + chunk metadata (mandatory at ingestion — FR-2)
# ---------------------------------------------------------------------------

class DocType(str, Enum):
    qp = "qp"                    # Qualification Pack
    nos = "nos"                  # National Occupational Standard extract
    scheme = "scheme"            # scheme guideline
    course = "course"            # course description/metadata
    faq = "faq"
    policy = "policy"            # NSQF descriptors, NCVET norms etc.
    taxonomy = "taxonomy"        # DGT trade list, crosswalk sources


class DocumentMeta(BaseModel):
    """Mandatory metadata for every ingested document. Ingestion rejects
    documents missing any required field (fail closed)."""
    doc_id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-_.]+$")
    title: str
    source_org: str                      # e.g. "HSSC / NQR", "MSDE"
    source_url: Optional[str] = None
    doc_type: DocType
    sector: str = "healthcare"
    language: Literal["en", "hi"] = "en"
    version: str = "1.0"
    license: str = "public"
    last_updated: Optional[str] = None   # ISO date from the source doc
    sensitivity: Sensitivity = Sensitivity.public
    allowed_roles: list[Role] = Field(default_factory=lambda: list(Role))
    allowed_purposes: list[Purpose] = Field(default_factory=lambda: list(Purpose))

    @field_validator("allowed_roles", "allowed_purposes")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("must not be empty — fail closed, not open")
        return v


class ChunkPayload(BaseModel):
    """Payload stored alongside each vector in Qdrant and each BM25 entry.
    Inherits governance fields from DocumentMeta so the RBAC pre-filter
    can act on the chunk without a join."""
    chunk_id: str                        # "{doc_id}#s{section_no}-c{n}"
    doc_id: str
    section: str = ""
    page: Optional[int] = None
    start_ts: Optional[float] = None     # seconds — video-transcript chunks only
    end_ts: Optional[float] = None       # seconds — video-transcript chunks only
    text: str
    chunk_hash: str                      # sha256 of normalised text
    language: Literal["en", "hi"] = "en"
    sensitivity: Sensitivity = Sensitivity.public
    allowed_roles: list[Role]
    allowed_purposes: list[Purpose]
    kg_node_ids: list[str] = Field(default_factory=list)
    index_manifest_id: str = ""

    # -- source attribution, captured at ingest time (denormalized from
    #    DocumentMeta/SOURCE_REGISTER.csv so a chunk carries its own
    #    provenance even if inspected standalone or the register changes) --
    source_org: str = ""
    source_url: Optional[str] = None
    source_license: str = "public"
    source_last_updated: Optional[str] = None

    # -- ingest-time OCR provenance (ST-legacy-docs) --
    source_mode: Literal["native", "ocr"] = "native"
    ocr_confidence: Optional[float] = None

    # -- ingest-time quality tagging (ST-dedup) --
    quality_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Citation contract (FR-7) — the LLM's output schema
# ---------------------------------------------------------------------------

class Claim(BaseModel):
    """One factual assertion in the answer, bound to its evidence."""
    text: str
    citation_ids: list[str] = Field(default_factory=list)   # chunk_ids
    kg_node_ids: list[str] = Field(default_factory=list)

    @property
    def is_cited(self) -> bool:
        return bool(self.citation_ids or self.kg_node_ids)


class RefusalReason(str, Enum):
    no_evidence = "no_evidence"
    out_of_role = "out_of_role"
    no_consent = "no_consent"
    unsafe_request = "unsafe_request"
    malformed_output = "malformed_output"


class CitationContract(BaseModel):
    """Structured response every surface must emit. The citation hard gate
    (gates.enforce_citations) validates this before anything reaches the
    user. KPI 7.2.4: citation completeness — every Claim must be cited."""
    schema_version: str = SCHEMA_VERSION
    answer_markdown: str
    claims: list[Claim] = Field(default_factory=list)
    kg_paths: list[list[str]] = Field(default_factory=list)  # ordered node-id paths
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    refusal_reason: Optional[RefusalReason] = None
    language: Literal["en", "hi"] = "en"
    index_manifest_id: str = ""

    @property
    def is_refusal(self) -> bool:
        return self.refusal_reason is not None

    def uncited_claims(self) -> list[Claim]:
        return [c for c in self.claims if not c.is_cited]


# ---------------------------------------------------------------------------
# Canonical skilling event (FR-5 / RFP 4.B.1f)
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    enrolment = "enrolment"
    attendance = "attendance"
    assessment = "assessment"
    certification = "certification"
    placement = "placement"


class SkillingEvent(BaseModel):
    """Canonical event schema for the transactional skilling-intelligence
    layer. PoC data is 100% synthetic (source_system=synthetic-gen)."""
    event_id: str
    event_type: EventType
    ts: datetime
    learner_id: str = Field(pattern=r"^SYN-")   # synthetic-only guard for PoC
    centre_id: str
    district: str
    state: str
    scheme_id: str
    course_id: str
    qp_code: str
    payload: dict[str, Any] = Field(default_factory=dict)
    consent_token_id: str = ""
    source_system: str = "synthetic-gen-v1"
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Consent token (PRD §8.5) — thin wrapper over phase6e consent ledger entries
# ---------------------------------------------------------------------------

class ConsentToken(BaseModel):
    consent_token_id: str
    user_id: str
    purpose: Purpose
    scope: list[str] = Field(default_factory=lambda: ["profile_basic", "query_history_session"])
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def valid_for(self, purpose: Purpose, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(self.issued_at.tzinfo)
        return (not self.revoked) and self.purpose == purpose and now < self.expires_at


# ---------------------------------------------------------------------------
# Gold eval item (PRD §8.6 / FR-22)
# ---------------------------------------------------------------------------

class EvalCategory(str, Enum):
    factual = "factual"
    refusal = "refusal"
    rbac = "rbac"
    injection = "injection"
    safety = "safety"


class GoldEvalItem(BaseModel):
    eval_id: str
    lang: Literal["en", "hi"]
    category: EvalCategory
    persona: Role
    query: str
    expected_behavior: Literal["answer", "refuse"]
    must_cite_docs: list[str] = Field(default_factory=list)
    reference_answer: str = ""
    rubric_notes: str = ""
    status: Literal["draft", "sme_reviewed"] = "draft"


# ---------------------------------------------------------------------------
# Structured (KG-node) embeddings — distinct from ChunkPayload's unstructured
# free-text chunks. See docs/design/design_and_setup_decisions.md §4 for the
# structured-vs-unstructured boundary this schema sits on.
# ---------------------------------------------------------------------------

class KGNodePayload(BaseModel):
    """One embeddable rendering of a Knowledge-Graph node (QP, NOS, Course,
    JobRole, TrainingCentre, Scheme, ...). Read directly from the curated
    KG CSVs (backend/substrate/kg/curated/) — the same source of truth
    backend.substrate.kg.loader loads into Neo4j — so this doesn't require
    a live Neo4j connection to produce.
    """
    node_id: str                         # e.g. "qp:HSS/Q5101", "course:crs-gda-01"
    node_type: str                       # QualificationPack | NOS | Course | JobRole | ...
    label: str                           # human-readable title/name
    text: str                            # the rendered text that was embedded
    source_doc_id: Optional[str] = None  # links back to the unstructured corpus, if any
    attrs: dict[str, Any] = Field(default_factory=dict)  # raw CSV row, for display/debug
    kg_content_hash: str = ""            # kg.loader.content_hash() at embed time
    index_manifest_id: str = ""
