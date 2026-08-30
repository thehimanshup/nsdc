"""Answer composer — emits the structured CitationContract (ST-501, FR-7).

Primary path: instruct the LLM (via the phase6e provider adapter) to return
STRICT JSON matching the contract. One retry on malformed output.

Fallback path (mock provider / repeated malformed output): EXTRACTIVE
composition — quote the top evidence chunks directly, each quote becoming a
cited claim. Extractive answers are conservative but 100% grounded by
construction, which keeps the offline demo honest.
"""
from __future__ import annotations

import json
import logging
import re

from .gates import EvidenceBundle
from .schemas import CitationContract, Claim

log = logging.getLogger("substrate.composer")

CONTRACT_INSTRUCTIONS = """You must respond with ONLY a JSON object, no prose around it:
{
  "answer_markdown": "<the answer, in the user's language>",
  "claims": [
    {"text": "<one factual assertion from the answer>",
     "citation_ids": ["<chunk_id supporting it>"],
     "kg_node_ids": ["<kg node id if applicable>"]}
  ],
  "confidence": <0.0-1.0>
}
Rules:
- Every factual assertion in answer_markdown MUST appear in claims with at
  least one citation_id drawn from the EVIDENCE chunk ids provided.
- Use ONLY the evidence provided. If evidence is insufficient for part of
  the question, say so in the answer instead of guessing.
- Do not invent chunk ids, courses, fees, dates or eligibility rules."""


def build_messages(question: str, bundle: EvidenceBundle,
                   agent_system: str, language: str) -> list[dict]:
    evidence_lines = []
    for chunk, score in bundle.chunks:
        evidence_lines.append(
            f"[chunk_id={chunk.chunk_id} | doc={chunk.doc_id} | section={chunk.section}"
            f" | kg={','.join(chunk.kg_node_ids) or '-'}]\n{chunk.text}")
    kg_block = ""
    if bundle.kg_paths:
        kg_block = "\nKG PATHWAYS:\n" + "\n".join(
            " -> ".join(p) for p in bundle.kg_paths[:5])
    system = (f"{agent_system}\n\n{CONTRACT_INSTRUCTIONS}\n\n"
              f"Answer language: {'Hindi' if language == 'hi' else 'English'}")
    user = (f"QUESTION:\n{question}\n\nEVIDENCE:\n"
            + "\n\n".join(evidence_lines) + kg_block)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def parse_contract(raw: str, bundle: EvidenceBundle,
                   language: str) -> CitationContract | None:
    """Parse LLM output into a contract; None if malformed. Citation ids not
    present in the evidence are stripped (anti-citation-spoofing)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        valid_ids = {c.chunk_id for c, _ in bundle.chunks}
        valid_kg = set(bundle.kg_node_ids) | {
            n for c, _ in bundle.chunks for n in c.kg_node_ids}
        claims = []
        for cl in data.get("claims", []):
            claims.append(Claim(
                text=str(cl.get("text", ""))[:500],
                citation_ids=[i for i in cl.get("citation_ids", []) if i in valid_ids],
                kg_node_ids=[i for i in cl.get("kg_node_ids", []) if i in valid_kg]))
        return CitationContract(
            answer_markdown=str(data.get("answer_markdown", "")).strip(),
            claims=claims,
            kg_paths=bundle.kg_paths,
            confidence=float(data.get("confidence", 0.5)),
            language=language,
            index_manifest_id=bundle.index_manifest_id)
    except Exception as e:
        log.info("contract parse failed: %s", e)
        return None


def extractive_contract(question: str, bundle: EvidenceBundle,
                        language: str, max_chunks: int = 3) -> CitationContract:
    """Deterministic grounded fallback: quote the best evidence verbatim."""
    top = bundle.chunks[:max_chunks]
    if language == "hi":
        intro = "मेरे ज्ञान-आधार के प्रमाणों के अनुसार:"
        outro = "\n\n_(यह उत्तर स्रोत-अंशों से संकलित है — प्रत्येक अंश का उद्धरण संलग्न है।)_"
    else:
        intro = "According to the evidence in my knowledge base:"
        outro = "\n\n_(Answer compiled from source passages — citation attached to each.)_"
    parts, claims = [intro], []
    for chunk, _ in top:
        quote = chunk.text[:400].rstrip() + ("…" if len(chunk.text) > 400 else "")
        attribution = f"{chunk.source_org}, {chunk.doc_id}, {chunk.section}" if chunk.source_org \
            else f"{chunk.doc_id}, {chunk.section}"
        parts.append(f"\n> {quote}\n> — *{attribution}*")
        claims.append(Claim(text=quote[:200], citation_ids=[chunk.chunk_id],
                            kg_node_ids=chunk.kg_node_ids))
    return CitationContract(
        answer_markdown="\n".join(parts) + outro,
        claims=claims, kg_paths=bundle.kg_paths,
        confidence=0.4,  # conservative — extractive, not synthesised
        language=language, index_manifest_id=bundle.index_manifest_id)


async def compose(llm, question: str, bundle: EvidenceBundle,
                  agent_system: str, language: str = "en",
                  max_attempts: int = 2) -> tuple[CitationContract, str]:
    """Returns (contract, mode) where mode ∈ {llm, llm-retry, extractive}."""
    messages = build_messages(question, bundle, agent_system, language)
    for attempt in range(max_attempts):
        try:
            raw = await llm.chat_complete(messages=messages, temperature=0.1,
                                          max_tokens=900, json_mode=True)
        except Exception as e:
            log.warning("llm compose failed (%s) — extractive fallback", e)
            break
        contract = parse_contract(raw, bundle, language)
        if contract and contract.answer_markdown:
            return contract, ("llm" if attempt == 0 else "llm-retry")
        messages.append({"role": "assistant", "content": raw[:800]})
        messages.append({"role": "user", "content":
                        "Invalid. Reply with ONLY the JSON object as specified."})
    return extractive_contract(question, bundle, language), "extractive"
