"""Evidence gate + citation hard gate — PRD FR-8/FR-9 (ST-502/ST-503).

Two synchronous gates on the generation path:

  evidence_gate()     BEFORE the composer — decides on retrieval scores
                      whether there is enough evidence to answer at all.
                      Refusals are decided here, not by model honesty.

  enforce_citations() AFTER the composer — validates the CitationContract:
                      every claim must carry at least one citation.
                      One retry is allowed; then the answer is blocked
                      and replaced with a safe refusal.

The async groundedness judge (ST-504) lives in evals/, not here — it
scores but never blocks, protecting the latency KPI (7.2.1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .schemas import Claim, CitationContract, RefusalReason

log = logging.getLogger("substrate.gates")

# Tunable thresholds — surfaced in .env later; conservative defaults.
MIN_TOP_SCORE = 0.35          # best fused retrieval score must exceed this
MIN_EVIDENCE_ITEMS = 1        # at least N chunks/KG hits above floor
SCORE_FLOOR = 0.20


@dataclass
class EvidenceBundle:
    """Output of the hybrid retriever fusion step (ST-403)."""
    chunks: list = field(default_factory=list)        # [(ChunkPayload, score)]
    kg_node_ids: list[str] = field(default_factory=list)
    kg_paths: list[list[str]] = field(default_factory=list)
    index_manifest_id: str = ""

    @property
    def top_score(self) -> float:
        return max((s for _, s in self.chunks), default=0.0)

    def strong_items(self, floor: float = SCORE_FLOOR) -> int:
        return sum(1 for _, s in self.chunks if s >= floor) + len(self.kg_paths)


@dataclass
class GateDecision:
    allowed: bool
    refusal_reason: Optional[RefusalReason] = None
    detail: str = ""


_SUPERLATIVE = None


def superlative_gate(question: str, bundle: EvidenceBundle) -> GateDecision:
    """Comparative/superlative asks ('best centre', 'highest placement')
    require comparative evidence — rankings, rates across entities — which
    document corpora rarely contain. Refuse unless the evidence bundle
    includes KG paths or multiple entities' quantitative data (closes G-026)."""
    global _SUPERLATIVE
    if _SUPERLATIVE is None:
        import re
        _SUPERLATIVE = re.compile(
            r"\b(best|worst|top|highest|lowest|most successful|number one|"
            r"सबसे (अच्छा|बेहतर|खराब|ऊँचा))\b", re.I)
    if not _SUPERLATIVE.search(question):
        return GateDecision(True)
    # comparative evidence heuristic: ≥2 distinct docs with numeric content
    numeric_docs = {c.doc_id for c, _ in bundle.chunks
                    if any(ch.isdigit() for ch in c.text) and "%" in c.text}
    if len(numeric_docs) >= 2:
        return GateDecision(True, detail="comparative numeric evidence present")
    log.info("superlative gate REFUSED: no comparative evidence for %.60s", question)
    return GateDecision(False, RefusalReason.no_evidence,
                        "superlative question without comparative evidence")


def evidence_gate(bundle: EvidenceBundle,
                  min_top_score: float = MIN_TOP_SCORE,
                  min_items: int = MIN_EVIDENCE_ITEMS) -> GateDecision:
    """Decide, on retrieval evidence alone, whether composition may proceed."""
    if bundle.top_score < min_top_score or bundle.strong_items() < min_items:
        detail = (f"top_score={bundle.top_score:.3f} (min {min_top_score}), "
                  f"strong_items={bundle.strong_items()} (min {min_items})")
        log.info("evidence gate REFUSED: %s", detail)
        return GateDecision(False, RefusalReason.no_evidence, detail)
    return GateDecision(True)


def no_evidence_contract(language: str = "en",
                         nearest_alternative: str = "",
                         manifest_id: str = "") -> CitationContract:
    """The grounded-refusal response (US-1.3 / T4)."""
    if language == "hi":
        msg = ("इस प्रश्न के लिए मेरे ज्ञान-आधार में पर्याप्त प्रमाण उपलब्ध नहीं है, "
               "इसलिए मैं अनुमान नहीं लगाऊँगा।")
        if nearest_alternative:
            msg += f"\n\nनिकटतम प्रमाणित विकल्प: {nearest_alternative}"
    else:
        msg = ("I don't have sufficient evidence in my knowledge base to answer "
               "this reliably, so I won't guess.")
        if nearest_alternative:
            msg += f"\n\nNearest grounded alternative: {nearest_alternative}"
    return CitationContract(answer_markdown=msg, claims=[], confidence=0.0,
                            refusal_reason=RefusalReason.no_evidence,
                            language=language, index_manifest_id=manifest_id)


_DISCRIMINATORY = None


def safety_gate(question: str) -> GateDecision:
    """Pre-retrieval screen for discriminatory-filtering requests (ST-521,
    PRD FR-21). Injection fencing is handled separately by prompt_safety;
    this catches selection/exclusion asks on protected attributes."""
    global _DISCRIMINATORY
    if _DISCRIMINATORY is None:
        import re
        _DISCRIMINATORY = re.compile(
            r"(only\s+(male|female|men|women|boys|girls)\b"
            r"|exclude\s+(women|men|sc|st|obc|muslim|hindu|christian|dalit)"
            r"|(prefer|recommend|select|shortlist)[^.]{0,40}"
            r"(caste|religion|general\s+category|upper\s+caste)"
            r"|(caste|जाति|धर्म)[^.]{0,30}(better|prefer|only|श्रेष्ठ|बेहतर|सिर्फ|केवल)"
            r"|(सिर्फ|केवल)[^.]{0,20}(पुरुष|महिला|जाति|लड़क))", re.I)
    if _DISCRIMINATORY.search(question):
        log.warning("safety gate REFUSED discriminatory request: %.80s", question)
        return GateDecision(False, RefusalReason.unsafe_request,
                            "protected-attribute selection/exclusion request")
    global _PII_HARVEST
    if _PII_HARVEST is None:
        import re
        _PII_HARVEST = re.compile(
            r"(list|show|give|reveal|export|share)[^.]{0,50}"
            r"(aadhaar|आधार|pan numbers?|phone numbers?|mobile numbers?|"
            r"bank accounts?|passwords?|personal data|contact details)"
            r"|(aadhaar|आधार)\s*(numbers?|संख्या|नंबर)", re.I)
    if _PII_HARVEST.search(question):
        log.warning("safety gate REFUSED PII-harvest request: %.80s", question)
        return GateDecision(False, RefusalReason.no_consent,
                            "bulk personal-identifier request")
    global _AUTHORITY
    if _AUTHORITY is None:
        import re
        _AUTHORITY = re.compile(
            r"\b(certify|attest|guarantee|officially\s+(approve|confirm)|"
            r"प्रमाणित\s+कर|गारंटी)\b", re.I)
    if _AUTHORITY.search(question):
        log.info("safety gate REFUSED authority request: %.80s", question)
        return GateDecision(False, RefusalReason.unsafe_request,
                            "certification/guarantee request — assistant drafts "
                            "and analyses; it never certifies, approves or "
                            "guarantees outcomes")
    return GateDecision(True)


_AUTHORITY = None


_PII_HARVEST = None


def unsafe_request_contract(language: str = "en", manifest_id: str = "",
                            reason: "RefusalReason" = None,
                            detail: str = "") -> CitationContract:
    if "certif" in detail or "guarantee" in detail:
        msg = ("मैं प्रमाणित या गारंटी नहीं कर सकता — मैं केवल विश्लेषण और ड्राफ़्ट "
               "तैयार करता हूँ। प्रमाणन NCVET/सक्षम प्राधिकरण का अधिकार है।") \
            if language == "hi" else \
              ("I can't certify, approve or guarantee anything — I only analyse "
               "and draft for human review. Certification and approval rest with "
               "NCVET / the competent authority.")
        return CitationContract(answer_markdown=msg, claims=[], confidence=1.0,
                                refusal_reason=RefusalReason.unsafe_request,
                                language=language, index_manifest_id=manifest_id)
    if reason == RefusalReason.no_consent:
        msg = ("मैं व्यक्तिगत पहचान-डेटा (आधार, फ़ोन, बैंक विवरण) साझा नहीं कर सकता। "
               "यह अनुरोध लॉग किया गया है।") if language == "hi" else \
              ("I can't share personal identifiers (Aadhaar, phone, bank details) — "
               "access requires valid role, purpose and consent. This request has "
               "been logged.")
        return CitationContract(answer_markdown=msg, claims=[], confidence=1.0,
                                refusal_reason=RefusalReason.no_consent,
                                language=language, index_manifest_id=manifest_id)
    if language == "hi":
        msg = ("मैं जाति, लिंग या धर्म के आधार पर चयन या बहिष्करण की सिफ़ारिश नहीं कर "
               "सकता। पात्रता केवल योजना के प्रकाशित मानदंडों (आयु, शिक्षा, दस्तावेज़) "
               "पर आधारित होती है — मैं उन मानदंडों के आधार पर मदद कर सकता हूँ।")
    else:
        msg = ("I can't recommend selecting or excluding candidates based on "
               "caste, gender or religion. Eligibility is determined only by the "
               "scheme's published criteria (age, education, documents) — I can "
               "help you apply those criteria instead.")
    return CitationContract(answer_markdown=msg, claims=[], confidence=1.0,
                            refusal_reason=RefusalReason.unsafe_request,
                            language=language, index_manifest_id=manifest_id)


def enforce_citations(contract: CitationContract) -> GateDecision:
    """Citation hard gate — KPI 7.2.4. Refusals pass (they claim nothing);
    any answer with at least one uncited claim fails."""
    if contract.is_refusal:
        return GateDecision(True, detail="refusal — no claims to cite")
    if not contract.claims:
        # An answer with prose but zero extracted claims is suspicious:
        # either it is pure conversational filler (fine) or the model
        # skipped claim extraction (not fine). Fail closed if the answer
        # contains factual-looking content markers.
        if any(tok in contract.answer_markdown
               for tok in ("NSQF", "₹", "eligib", "QP", "NOS", "scheme", "course")):
            return GateDecision(False, RefusalReason.malformed_output,
                                "factual content with no extracted claims")
        return GateDecision(True, detail="no factual claims")
    uncited = contract.uncited_claims()
    if uncited:
        detail = f"{len(uncited)}/{len(contract.claims)} claims uncited: " + \
                 "; ".join(c.text[:60] for c in uncited[:3])
        log.warning("citation gate FAILED: %s", detail)
        return GateDecision(False, RefusalReason.malformed_output, detail)
    return GateDecision(True)


def blocked_output_contract(language: str = "en", manifest_id: str = "") -> CitationContract:
    """Safe fallback when regeneration also fails the citation gate."""
    msg = ("मैं इस उत्तर को प्रमाणों से सत्यापित नहीं कर सका, इसलिए इसे रोक दिया गया है। "
           "कृपया प्रश्न को दोबारा या अधिक विशिष्ट रूप से पूछें।") if language == "hi" else \
          ("I generated an answer but could not verify every statement against "
           "my sources, so it was withheld. Please rephrase or narrow the question.")
    return CitationContract(answer_markdown=msg, claims=[], confidence=0.0,
                            refusal_reason=RefusalReason.malformed_output,
                            language=language, index_manifest_id=manifest_id)
