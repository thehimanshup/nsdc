"""Substrate API routes — mounted when SUBSTRATE_RAG=true (ST-404/601/702).

POST /api/v1/substrate/auth/login        demo login → signed role token
POST /api/v1/substrate/query             governed Graph-RAG query (Bearer)
POST /api/v1/substrate/copilot/analytics jurisdiction-scoped analytics (Bearer, officer)
GET  /api/v1/substrate/registry          index manifests + KG releases
GET  /api/v1/substrate/health            leg availability

Role and jurisdiction come ONLY from the verified bearer token (ST-601).
Set SUBSTRATE_AUTH_OPTIONAL=true for keyless local smoke tests — requests
then run as the demo learner unless a token is presented.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .substrate.authn import (AuthError, DEMO_USERS, Principal, issue_token,
                              principal_from_header)
from .substrate.schemas import Purpose, Role

logger = logging.getLogger("substrate.routes")
from .substrate.service import get_service

router = APIRouter(prefix="/api/v1/substrate", tags=["substrate"])

_UI_PATH = Path(__file__).resolve().parents[1] / "web" / "substrate.html"

ui_router = APIRouter(tags=["substrate-ui"])


@ui_router.get("/substrate-demo", include_in_schema=False)
async def substrate_demo_ui():
    from fastapi.responses import FileResponse
    return FileResponse(_UI_PATH, media_type="text/html")

DEFAULT_PURPOSE = {
    "mentor": Purpose.course_guidance,
    "officer_copilot": Purpose.scheme_admin,
    "content_qa": Purpose.content_qa,
}

AGENT_ALLOWED_ROLES = {
    "mentor": {Role.learner, Role.admin},
    "officer_copilot": {Role.officer, Role.admin},
    "content_qa": {Role.sme, Role.admin},
}


def _principal(authorization: Optional[str]) -> Principal:
    if authorization:
        try:
            return principal_from_header(authorization)
        except AuthError as e:
            raise HTTPException(401, str(e))
    if os.getenv("SUBSTRATE_AUTH_OPTIONAL", "").lower() in ("1", "true", "yes"):
        return Principal(sub="anonymous-dev", name="Dev (no token)",
                         role=Role.learner)
    raise HTTPException(401, "bearer token required — POST /auth/login first")


# ------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(req: LoginRequest) -> dict:
    try:
        token = issue_token(req.username, req.password)
    except AuthError:
        raise HTTPException(401, "invalid credentials")
    u = DEMO_USERS[req.username]
    return {"token": token, "name": u["name"], "role": u["role"],
            "jurisdiction": u["jurisdiction"]}


@router.get("/auth/demo-users")
async def demo_users() -> dict:
    return {"users": [{"username": k, "role": v["role"],
                       "jurisdiction": v["jurisdiction"]}
                      for k, v in DEMO_USERS.items()]}


# ------------------------------------------------------------------ query
class SubstrateQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    agent_id: str = "mentor"
    language: Optional[str] = None      # auto-detected when omitted


@router.post("/query")
async def substrate_query(req: SubstrateQueryRequest,
                          authorization: Optional[str] = Header(default=None)
                          ) -> dict:
    principal = _principal(authorization)
    allowed = AGENT_ALLOWED_ROLES.get(req.agent_id, set())
    if principal.role not in allowed:
        raise HTTPException(403, f"role '{principal.role.value}' may not use "
                                 f"agent '{req.agent_id}' — attempt logged")
    purpose = DEFAULT_PURPOSE.get(req.agent_id, Purpose.course_guidance)
    svc = get_service()
    result = await svc.query(req.question, principal.role, purpose,
                             agent_id=req.agent_id, language=req.language,
                             actor=principal.sub)
    c = result.contract
    return {
        "interaction_id": result.interaction_id,
        "actor": principal.sub, "role": principal.role.value,
        "answer_markdown": c.answer_markdown,
        "claims": [cl.model_dump() for cl in c.claims],
        "kg_paths": c.kg_paths,
        "confidence": c.confidence,
        "refusal_reason": c.refusal_reason.value if c.refusal_reason else None,
        "language": c.language,
        "index_manifest_id": c.index_manifest_id,
        "latency_ms": result.latency_ms,
        "compose_mode": result.compose_mode,
        "gates": result.gates,
        "retrieved": result.retrieval["chunks"],
    }


# ----------------------------------------------------------- retrieval
class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    mode: str = "hybrid"                       # bm25 | vector | hybrid
    language: Optional[str] = None             # "en" | "hi"
    doc_ids: Optional[list[str]] = None
    section_contains: Optional[str] = None
    source_mode: Optional[str] = None          # "native" | "ocr"
    purpose: Optional[str] = None              # defaults per role


@router.post("/retrieve")
async def retrieve_endpoint(req: RetrieveRequest,
                             authorization: Optional[str] = Header(default=None)
                             ) -> dict:
    """Standalone retrieval: ranked evidence with source attribution and
    the immutable index_manifest_id on every result — no answer composition.
    Hybrid (BM25 + vector, RRF-fused) by default; degrades to bm25 with an
    explicit `legs_used` when no vector index is available."""
    from .substrate import retrieval_api

    principal = _principal(authorization)
    try:
        purpose = Purpose(req.purpose) if req.purpose else \
            DEFAULT_PURPOSE.get("mentor", Purpose.course_guidance)
    except ValueError:
        raise HTTPException(422, f"unknown purpose: {req.purpose}")

    vector_store = None
    if req.mode in ("vector", "hybrid"):
        if Path("data/qdrant_local").exists():
            try:
                from .substrate.vector_store import VectorStore, get_embedder
                embed, dim, _name = get_embedder()
                vector_store = VectorStore(embed, dim, path="data/qdrant_local")
            except Exception as e:   # noqa: BLE001 - leg is optional
                logger.warning("vector leg unavailable: %s", e)

    try:
        return retrieval_api.retrieve(
            req.query, principal.role, purpose,
            top_k=req.top_k, mode=req.mode, language=req.language,
            doc_ids=req.doc_ids, section_contains=req.section_contains,
            source_mode=req.source_mode, vector_store=vector_store)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


# --------------------------------------------------------- supervisor
class SupervisorQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    agent_id: Optional[str] = None      # hint only — supervisor may override
    language: Optional[str] = None


@router.post("/supervisor/query")
async def supervisor_query(req: SupervisorQueryRequest,
                            authorization: Optional[str] = Header(default=None)
                            ) -> dict:
    """Cross-persona entry point: classifies which persona should actually
    handle the question, runs it, attaches a live groundedness flag if the
    rolling hallucination rate is over threshold, and escalates to the
    officer queue if the answer was blocked or officer-track.

    Unlike /query, the caller does not have to know which of the three
    personas is right — this route figures that out.
    """
    from .substrate import supervisor as sup

    principal = _principal(authorization)
    routing = sup.classify(req.question, principal.role, req.agent_id)

    purpose = DEFAULT_PURPOSE.get(routing.persona, Purpose.course_guidance)
    svc = get_service()
    result = await svc.query(req.question, principal.role, purpose,
                             agent_id=routing.persona, language=req.language,
                             actor=principal.sub)
    c = result.contract

    flag = sup.quality_gate()
    escalation = sup.maybe_escalate(
        principal, req.question, routing,
        c.refusal_reason.value if c.refusal_reason else None,
    )

    return {
        "interaction_id": result.interaction_id,
        "actor": principal.sub, "role": principal.role.value,
        "routing": {
            "persona": routing.persona,
            "reason": routing.reason,
            "requested_persona": routing.requested_persona,
            "overridden": routing.overridden,
        },
        "answer_markdown": c.answer_markdown,
        "claims": [cl.model_dump() for cl in c.claims],
        "confidence": c.confidence,
        "refusal_reason": c.refusal_reason.value if c.refusal_reason else None,
        "language": c.language,
        "latency_ms": result.latency_ms,
        "gates": result.gates,
        "governance_flag": flag,
        "escalation": {
            "escalated": escalation.escalated,
            "escalation_id": escalation.escalation_id,
            "note": escalation.note,
        },
    }


@router.get("/supervisor/escalations")
async def supervisor_escalations(status: Optional[str] = None,
                                  authorization: Optional[str] = Header(default=None)
                                  ) -> dict:
    """Officer/admin-facing view of the supervisor's escalation queue."""
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin):
        raise HTTPException(403, "no escalation-queue access for this role")
    from .substrate import supervisor as sup
    return {"escalations": sup.list_escalations(status=status)}


# -------------------------------------------------------------- analytics
class AnalyticsRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/copilot/analytics")
async def copilot_analytics(req: AnalyticsRequest,
                            authorization: Optional[str] = Header(default=None)
                            ) -> dict:
    principal = _principal(authorization)
    from .substrate.analytics import run_analytics
    result = run_analytics(req.question, principal,
                           data_dir=os.getenv("DATA_DIR", "data"))
    # audit — including refusals (out-of-scope attempts must be visible)
    try:
        from .audit import append_event
        append_event(actor=principal.sub, action="substrate.analytics",
                     resource={"template": result.template or "none"},
                     payload={"role": principal.role.value,
                              "question": req.question[:300],
                              "scope": result.scope,
                              "refused": result.refused,
                              "refusal_detail": result.refusal_detail,
                              "row_count": len(result.rows)})
    except Exception:
        pass
    if result.refused:
        raise HTTPException(403 if "role" in result.refusal_detail else 422,
                            result.refusal_detail)
    return {"template": result.template, "answer": result.answer_text,
            "rows": result.rows, "sql": result.sql, "scope": result.scope,
            "note": "DRAFT analytics for human review — synthetic data"}


# ------------------------------------------------------------ draft notes
class DraftRequest(BaseModel):
    subject: str = Field(default="Monthly scheme status", max_length=200)


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = Field(default="", max_length=500)


def _audit_event(actor, action, resource, payload):
    try:
        from .audit import append_event
        append_event(actor=actor, action=action, resource=resource, payload=payload)
    except Exception:
        pass


@router.post("/copilot/draft-note")
async def create_draft_note(req: DraftRequest,
                            authorization: Optional[str] = Header(default=None)
                            ) -> dict:
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin):
        raise HTTPException(403, "only officers may generate draft notes")
    from .substrate.drafts import generate_note
    draft = generate_note(principal, os.getenv("DATA_DIR", "data"), req.subject)
    _audit_event(principal.sub, "substrate.draft.create",
                 {"draftId": draft["draft_id"]},
                 {"subject": req.subject, "jurisdiction": principal.jurisdiction,
                  "review_status": "pending"})
    return draft


@router.get("/copilot/draft-notes")
async def list_draft_notes(authorization: Optional[str] = Header(default=None)
                           ) -> dict:
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin):
        raise HTTPException(403, "only officers may view draft notes")
    from .substrate.drafts import list_notes
    return {"drafts": list_notes(principal, os.getenv("DATA_DIR", "data"))}


@router.post("/copilot/draft-notes/{draft_id}/decision")
async def decide_draft_note(draft_id: str, req: DecisionRequest,
                            authorization: Optional[str] = Header(default=None)
                            ) -> dict:
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin):
        raise HTTPException(403, "only officers may decide draft notes")
    from .substrate.drafts import decide
    try:
        d = decide(principal, draft_id, req.decision, req.comment,
                   os.getenv("DATA_DIR", "data"))
    except PermissionError as e:
        _audit_event(principal.sub, "substrate.draft.decision_denied",
                     {"draftId": draft_id}, {"reason": str(e)})
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if d is None:
        raise HTTPException(404, "draft not found")
    _audit_event(principal.sub, "substrate.draft.decision",
                 {"draftId": draft_id},
                 {"decision": req.decision, "comment": req.comment})
    return d


# --------------------------------------------------------------- content QA
class CoverageRequest(BaseModel):
    course_id: str
    qp_code: str


class ItemsRequest(BaseModel):
    nos_code: str
    count: int = Field(default=5, ge=1, le=10)
    bloom_max: int = Field(default=3, ge=1, le=6)


@router.post("/contentqa/coverage")
async def contentqa_coverage(req: CoverageRequest,
                             authorization: Optional[str] = Header(default=None)
                             ) -> dict:
    principal = _principal(authorization)
    if principal.role not in (Role.sme, Role.admin):
        raise HTTPException(403, "only SMEs may run coverage checks")
    from .substrate.contentqa import coverage_check
    result = coverage_check(req.course_id, req.qp_code)
    if "error" in result:
        raise HTTPException(422, result["error"])
    _audit_event(principal.sub, "substrate.contentqa.coverage",
                 {"course": req.course_id, "qp": req.qp_code},
                 {"covered": result["nos_covered"], "total": result["nos_total"],
                  "gaps": [g["nos_code"] for g in result["gaps"]]})
    return result


@router.post("/contentqa/items")
async def contentqa_items(req: ItemsRequest,
                          authorization: Optional[str] = Header(default=None)
                          ) -> dict:
    principal = _principal(authorization)
    if principal.role not in (Role.sme, Role.admin):
        raise HTTPException(403, "only SMEs may draft assessment items")
    from .substrate.contentqa import draft_items
    try:
        from .llm import get_llm
        llm = get_llm()
    except Exception:
        llm = None
    result = await draft_items(req.nos_code, req.count, req.bloom_max,
                               author=principal.sub,
                               data_dir=os.getenv("DATA_DIR", "data"), llm=llm)
    if "error" in result:
        raise HTTPException(422, result["error"])
    _audit_event(principal.sub, "substrate.contentqa.items",
                 {"nos": req.nos_code},
                 {"count": len(result["items"]), "mode": result["compose_mode"]})
    return result


# ---------------------------------------------------------------- feedback
class FeedbackRequest(BaseModel):
    interaction_id: str
    verdict: str = Field(pattern="^(up|down)$")
    comment: str = Field(default="", max_length=500)
    question: str = Field(default="", max_length=2000)
    answer_preview: str = Field(default="", max_length=500)


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest,
                          authorization: Optional[str] = Header(default=None)
                          ) -> dict:
    """ST-804: thumbs-down answers auto-queue as candidate gold-set items."""
    principal = _principal(authorization)
    import json as _json
    from datetime import datetime, timezone
    entry = {
        "feedback_id": "fb_" + __import__("uuid").uuid4().hex[:10],
        "interaction_id": req.interaction_id, "verdict": req.verdict,
        "comment": req.comment, "question": req.question,
        "answer_preview": req.answer_preview,
        "by": principal.sub, "role": principal.role.value,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    with (data_dir / "feedback.jsonl").open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    if req.verdict == "down":
        eval_dir = Path("evals")
        with (eval_dir / "gold_candidates.jsonl").open("a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "source": "user_feedback", "feedback_id": entry["feedback_id"],
                "persona": principal.role.value, "query": req.question,
                "observed_answer": req.answer_preview, "comment": req.comment,
                "status": "candidate — needs SME rubric + expected_behavior",
            }, ensure_ascii=False) + "\n")
    _audit_event(principal.sub, "substrate.feedback",
                 {"interactionId": req.interaction_id},
                 {"verdict": req.verdict, "queued_for_eval": req.verdict == "down"})
    return {"ok": True, "queued_for_eval": req.verdict == "down"}


# ------------------------------------------------------------------- audit
@router.get("/audit")
async def audit_trail(limit: int = 50,
                      authorization: Optional[str] = Header(default=None)
                      ) -> dict:
    """Recent substrate audit events. Admin sees all; officers see their own."""
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin, Role.sme):
        raise HTTPException(403, "no audit access for this role")
    events, chain = [], None
    try:
        from pathlib import Path as _P
        p = _P(os.getenv("DATA_DIR", "data")) / "audit" / "events.jsonl"
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            for ln in reversed(lines):
                e = json.loads(ln)
                if not str(e.get("action", "")).startswith("substrate."):
                    continue
                if principal.role != Role.admin and e.get("actor") != principal.sub:
                    continue
                events.append(e)
                if len(events) >= min(limit, 200):
                    break
        from .audit import verify_chain
        chain = verify_chain()
    except Exception as e:
        chain = {"ok": None, "error": str(e)}
    return {"events": events, "chain": chain}


# ---------------------------------------------------------------- registry
@router.get("/registry")
async def substrate_registry() -> dict:
    svc = get_service()
    manifests = [json.loads(m.model_dump_json()) for m in svc.registry.list_all()]
    kg_releases = []
    kg_dir = Path("data/kg_releases")
    if kg_dir.exists():
        kg_releases = [json.loads(p.read_text()) for p in sorted(kg_dir.glob("kg-*.json"))]
    return {"current_manifest": svc.registry.current_id(),
            "manifests": manifests, "kg_releases": kg_releases}


@router.get("/judge/stats")
async def judge_stats(authorization: Optional[str] = Header(default=None)) -> dict:
    """KPI 7.2.2 measurement surface — aggregated groundedness-judge results."""
    principal = _principal(authorization)
    if principal.role not in (Role.officer, Role.admin, Role.sme):
        raise HTTPException(403, "no judge-stats access for this role")
    from .substrate.judge import stats
    return stats(os.getenv("DATA_DIR", "data"))


@router.get("/health")
async def substrate_health() -> dict:
    svc = get_service()
    return {
        "manifest": svc.manifest_id or None,
        "legs": {
            "bm25": svc.retriever.bm25_retrieve is not None,
            "vector": svc.retriever.vs is not None,
            "kg": svc.retriever.kg_session_factory is not None,
        },
    }
