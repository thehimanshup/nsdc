"""Tool registry + DigiLocker mock implementations.

Tools are the typed, consent-aware actions an agent can take.

In Phase 2 all tools are stubbed — DigiLocker is mocked from JSON fixtures.
Phase 3 onwards swaps the implementation for real DigiLocker OAuth, Aadhaar
verification, UPI intents, etc. The Tool contract stays identical.

Lifecycle of a tool call:
  1. Agent (via Sarvam function-calling, or keyword in mock mode) requests
     a tool that has `requires_consent=True`.
  2. Orchestrator emits a `consent_request` WS frame to the citizen.
  3. Citizen taps "Allow" / "Deny" in the simulator modal.
  4. Citizen's decision is logged in the consent ledger.
  5. If allowed: tool.execute() is called; result is appended to the
     conversation context, agent generates final reply.
"""
from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .config import settings


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    id: str
    name: str
    description: str
    connector: str
    requires_consent: bool
    consent_scope: str
    input_schema: dict
    allowed_agents: list[str]
    execute: Callable[[dict, str], Awaitable[dict]]   # (args, citizen_id) -> result
    sla_p95_ms: int = 1500
    # Configurable-tools metadata (Phase 6 — configurable tools & MCP).
    #   category — groups tools on the admin Tools page (free text, e.g. "revenue").
    #   source   — where the tool came from: "builtin" | "plugin" | "mcp".
    # These are display/grouping metadata only; they never gate execution.
    category: str = ""
    source: str = "builtin"
    # Optional regex keyword triggers. The legacy orchestrator selects tools by
    # keyword match (Sarvam native function-calling isn't wired), so a drop-in
    # plugin / MCP tool is invisible to chat unless it declares how to recognise
    # the intent. Empty = not keyword-reachable (still usable via the Test
    # button and the LangGraph engine's function-calling).
    trigger_patterns: list[str] = field(default_factory=list)

    def to_function_schema(self) -> dict:
        """Convert to Sarvam/OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.id,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


_TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    _TOOLS[tool.id] = tool


def get_tool(tool_id: str) -> Optional[Tool]:
    return _TOOLS.get(tool_id)


def tools_for_agent(agent_id: str) -> list[Tool]:
    """Tools an agent may use on THIS turn.

    The operator-controlled bindings file (data/tool_bindings.json) wins:
      - a binding with enabled=False hides the tool from every agent;
      - a binding's `agents` list is the live wiring;
      - with no binding for a tool, we fall back to its in-code
        `allowed_agents` (the developer's `default_agents` suggestion).

    Both orchestration engines call this fresh on every message, so an
    enable/disable or rewire on the Tools page takes effect on the next turn.
    """
    from . import tool_bindings  # local import keeps tools.py import-cheap
    out: list[Tool] = []
    for t in _TOOLS.values():
        b = tool_bindings.get(t.id)
        if b is not None and not b.get("enabled", True):
            continue
        agents = b.get("agents") if b is not None else t.allowed_agents
        if agent_id in (agents or []):
            out.append(t)
    return out


def all_tools() -> list[Tool]:
    return list(_TOOLS.values())


# ---------------------------------------------------------------------------
# DigiLocker mock fixtures
# ---------------------------------------------------------------------------

_DEFAULT_FIXTURES = {
    "patta": {
        "patta_no": "1147/2014",
        "owner_name": "Citizen (mock)",
        "village": "Mylapore",
        "taluk": "Mylapore",
        "district": "Chennai",
        "survey_no": "{survey_no}",
        "extent_hectares": 0.85,
        "land_type": "Wet land",
        "tenure": "Owner",
        "issued_on": "2014-08-12",
        "issuing_authority": "Office of the Tahsildar, Mylapore",
        "is_mock": True,
    },
    "ec": {
        "property_id": "P-CHN-0142-001",
        "owner_name": "Citizen (mock)",
        "address": "21, North Mada Street, Mylapore, Chennai 600004",
        "transactions": [
            {"date": "2018-06-04", "type": "Sale Deed", "doc_no": "1842/2018",
             "consideration": 4500000, "buyer": "Citizen (mock)",
             "seller": "Previous Owner (mock)"}
        ],
        "ec_period": "2015-01-01 to 2025-12-31",
        "issued_on": "2025-05-26",
        "issuing_authority": "Sub-Registrar Office, Mylapore",
        "is_mock": True,
    },
    "dl": {
        "dl_number": "TN0119901234567",
        "name": "Citizen (mock)",
        "dob": "1988-04-12",
        "address": "Chennai, Tamil Nadu",
        "issued_on": "2019-04-30",
        "valid_until": "2039-04-29",
        "vehicle_classes": ["LMV", "MCWG"],
        "issuing_rto": "Chennai South RTO (TN-01)",
        "is_mock": True,
    },
    "ration_card": {
        "card_no": "TN-04-PHH-{n}",
        "category": "Priority Household",
        "head_of_family": "Citizen (mock)",
        "members": 4,
        "fps_shop_no": "S-1247",
        "village_ward": "Ward 142, Mylapore",
        "monthly_allocation": {"rice_kg": 40, "sugar_kg": 1},
        "aadhaar_seeded": True,
        "is_mock": True,
    },
}


def _fixture_path() -> Path:
    p = Path(settings.data_dir) / "digilocker_fixtures.json"
    if not p.exists():
        p = Path(__file__).resolve().parent.parent / "data" / "digilocker_fixtures.json"
    return p


def _load_fixtures() -> dict:
    p = _fixture_path()
    if not p.exists():
        return _DEFAULT_FIXTURES
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _DEFAULT_FIXTURES



def _mock_connector_disabled(connector: str) -> dict | None:
    """Return a hard failure when a tool has only a fixture implementation
    and production/mock fallbacks are disabled."""
    if not settings.allow_mock_providers:
        return {
            "ok": False,
            "error": "connector_not_configured",
            "message": (
                f"{connector} is not connected to a live government system. "
                "Mock fixture responses are disabled for this deployment."
            ),
            "is_mock": False,
        }
    return None

# ---------------------------------------------------------------------------
# Tool implementations (mock)
# ---------------------------------------------------------------------------

async def _digilocker_fetch_patta(args: dict, citizen_id: str) -> dict:
    disabled = _mock_connector_disabled("DigiLocker")
    if disabled:
        return disabled
    f = _load_fixtures().get("patta", _DEFAULT_FIXTURES["patta"])
    out = dict(f)
    out["survey_no"] = args.get("survey_no") or "243/2A"
    out["fetched_at"] = "2026-05-27T08:14:22Z"
    return {"ok": True, "document": out}


async def _digilocker_fetch_ec(args: dict, citizen_id: str) -> dict:
    disabled = _mock_connector_disabled("DigiLocker")
    if disabled:
        return disabled
    f = _load_fixtures().get("ec", _DEFAULT_FIXTURES["ec"])
    return {"ok": True, "document": dict(f)}


async def _digilocker_fetch_dl(args: dict, citizen_id: str) -> dict:
    disabled = _mock_connector_disabled("DigiLocker")
    if disabled:
        return disabled
    f = _load_fixtures().get("dl", _DEFAULT_FIXTURES["dl"])
    return {"ok": True, "document": dict(f)}


async def _digilocker_fetch_ration(args: dict, citizen_id: str) -> dict:
    disabled = _mock_connector_disabled("DigiLocker")
    if disabled:
        return disabled
    f = _load_fixtures().get("ration_card", _DEFAULT_FIXTURES["ration_card"])
    out = dict(f)
    out["card_no"] = f["card_no"].replace("{n}", str(random.randint(10000, 99999)))
    return {"ok": True, "document": out}


# ---------------------------------------------------------------------------
# Phase 6e — record-backed tools. The two legacy fire-and-forget tools
# (water.register_complaint, cmo.create_grievance) are rewired to create a
# real, trackable Record via records.service. New generic record/scheme/
# project tools sit alongside them.
# ---------------------------------------------------------------------------

def _citizen_ctx(citizen_id: str) -> dict:
    """Pull state / msisdn / district / language from the conversation store."""
    try:
        from .store import store
        c = store.get_citizen(citizen_id) or {}
        return {
            "msisdn": c.get("msisdn", ""),
            "state_code": c.get("state_code", "") or "TN",
            "district": c.get("district"),
            "lang": c.get("language", "en-IN"),
        }
    except Exception:
        return {"msisdn": "", "state_code": "TN", "district": None, "lang": "en-IN"}


def _create_record_from_args(args: dict, citizen_id: str, *,
                             department_id: str, default_category: str,
                             kind: str = "grievance") -> dict:
    from .records import service as rsvc
    from .records import sla as _sla
    from .records.store import records_store
    ctx = _citizen_ctx(citizen_id)
    category = args.get("category") or default_category
    title = args.get("title") or args.get("text") or category.replace("_", " ").title()
    desc = args.get("details") or args.get("description") or args.get("text") or ""
    if args.get("location"):
        desc = f"Location: {args['location']}. {desc}".strip()

    # Phase 6e — duplicate guard. The keyword matcher can fire on several
    # turns about the SAME problem ("the leak", "still leaking", "any update")
    # and would otherwise file a new complaint each time. If the citizen
    # already has an OPEN record in this department+category opened in the
    # last 10 minutes, return that one instead of creating a duplicate.
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(minutes=10)).isoformat()
    for ex in records_store.for_citizen(citizen_id, open_only=True):
        if (ex.department_id == department_id and ex.category == category
                and (ex.created_at or "") >= cutoff):
            return {
                "ok": True, "record_id": ex.record_id, "status": ex.status,
                "category": ex.category, "department": ex.department_id,
                "owner_desk": _sla.desk_label(ex.owner_desk_id),
                "level": ex.current_level, "sla_due_at": ex.sla_due_at,
                "track_at": f"/api/v1/track/{ex.record_id}",
                "duplicate": True,
                "message": (f"You already have an open complaint ({ex.record_id}) "
                            f"for this — I've added your note to it rather than "
                            f"opening a duplicate."),
                "is_mock": False,
            }

    rec = rsvc.create_record(
        kind=kind, citizen_id=citizen_id, msisdn=ctx["msisdn"],
        state_code=ctx["state_code"], department_id=department_id,
        category=category, title=str(title)[:160], description=str(desc),
        district=args.get("district") or ctx["district"],
        ward_block=args.get("ward_block"),
        channel=args.get("_channel", "simulator"), lang=ctx["lang"],
        priority=args.get("priority", "normal"),
        workflow_id=args.get("workflow_id"),
        parent_record_id=args.get("parent_record_id"),
        project_id=args.get("project_id"),
    )
    return {
        "ok": True,
        "record_id": rec.record_id,
        "status": rec.status,
        "category": rec.category,
        "department": rec.department_id,
        "owner_desk": _sla.desk_label(rec.owner_desk_id),
        "level": rec.current_level,
        "sla_due_at": rec.sla_due_at,
        "track_at": f"/api/v1/track/{rec.record_id}",
        "message": (f"Registered as {rec.record_id}. Assigned to "
                    f"{_sla.desk_label(rec.owner_desk_id)}. "
                    f"Track it anytime with this reference number."),
        "is_mock": False,
    }


async def _water_complaint_register(args: dict, citizen_id: str) -> dict:
    return _create_record_from_args(
        args, citizen_id, department_id="water",
        default_category=args.get("category", "leak"))


async def _grievance_create(args: dict, citizen_id: str) -> dict:
    return _create_record_from_args(
        args, citizen_id, department_id="cmo",
        default_category=args.get("category", "general"))


async def _records_create(args: dict, citizen_id: str) -> dict:
    dept = args.get("department_id") or args.get("agent_id") or "cmo"
    return _create_record_from_args(
        args, citizen_id, department_id=dept,
        default_category=args.get("category", "general"),
        kind=args.get("kind", "grievance"))


async def _records_track(args: dict, citizen_id: str) -> dict:
    from .records import service as rsvc
    rid = (args.get("record_id") or args.get("reference") or "").strip()
    if not rid:
        return {"ok": False, "error": "record_id required"}
    view = rsvc.track(rid)
    if not view:
        return {"ok": False, "error": "not_found",
                "message": f"No record found for {rid}. Please re-check the reference number."}
    return {"ok": True, "record": view}


async def _records_list_mine(args: dict, citizen_id: str) -> dict:
    from .records.store import records_store
    recs = records_store.for_citizen(citizen_id,
                                     open_only=bool(args.get("open_only")))
    return {"ok": True, "count": len(recs),
            "records": [r.public_view() for r in recs[:20]]}


async def _records_reminder(args: dict, citizen_id: str) -> dict:
    from .records.store import records_store
    from .records import service as rsvc
    rec = records_store.get(args.get("record_id", ""))
    if not rec or rec.citizen_id != citizen_id:
        return {"ok": False, "error": "not_found"}
    await rsvc.send_reminder(rec, actor=citizen_id)
    return {"ok": True, "record_id": rec.record_id, "priority": rec.priority,
            "reminders": rec.extra.get("reminders")}


async def _records_feedback(args: dict, citizen_id: str) -> dict:
    from .records.store import records_store
    from .records import service as rsvc
    rec = records_store.get(args.get("record_id", ""))
    if not rec or rec.citizen_id != citizen_id:
        return {"ok": False, "error": "not_found"}
    await rsvc.submit_feedback(rec, actor=citizen_id,
                               rating=int(args.get("rating", 3)),
                               comment=args.get("comment", ""))
    return {"ok": True, "record_id": rec.record_id, "status": rec.status,
            "satisfaction": rec.satisfaction}


# --- Agriculture market-price tool (Phase 6g) ------------------------------
#
# Validation: "Cannot answer real time price of any crop." There was no market
# data source at all. This tool queries data.gov.in's Agmarknet daily mandi
# price feed (official, free) with an in-process cache, and falls back to the
# published MSP table when the API is unreachable — always saying clearly
# which kind of price it is and for which date.

_MANDI_RESOURCE = "9ef84268-d588-465a-a308-a864a43d0070"  # daily mandi prices
_MANDI_CACHE: dict[str, tuple[float, dict]] = {}          # key -> (ts, result)
_MANDI_CACHE_TTL = 6 * 3600.0

# Minimum Support Prices, ₹/quintal (2025-26 season, Govt. of India press
# releases). FALLBACK ONLY — the live Agmarknet rate is always preferred.
_MSP_2025_26: dict[str, int] = {
    "paddy": 2369, "rice": 2369, "wheat": 2425, "maize": 2400,
    "jowar": 3699, "bajra": 2775, "ragi": 4886, "tur": 8000, "arhar": 8000,
    "moong": 8768, "urad": 7800, "groundnut": 7263, "soyabean": 5328,
    "soybean": 5328, "sunflower": 7721, "cotton": 7710, "sesamum": 9846,
    "nigerseed": 9537, "sugarcane": 355,
}

_STATE_NAMES = {
    "TN": "Tamil Nadu", "KA": "Karnataka", "MH": "Maharashtra",
    "UP": "Uttar Pradesh", "WB": "West Bengal", "AP": "Andhra Pradesh",
    "GJ": "Gujarat", "PB": "Punjab", "DL": "NCT of Delhi", "KL": "Kerala",
}


async def _mandi_price(args: dict, citizen_id: str) -> dict:
    import time
    import httpx
    commodity = (args.get("commodity") or "").strip()
    if not commodity:
        return {"ok": False, "error": "commodity_required",
                "message": "Ask the citizen which crop/commodity they mean."}
    ctx = _citizen_ctx(citizen_id)
    state = (args.get("state") or "").strip() or _STATE_NAMES.get(
        ctx.get("state_code", ""), "")
    market = (args.get("market") or "").strip()

    cache_key = f"{commodity.lower()}|{state.lower()}|{market.lower()}"
    now = time.time()
    hit = _MANDI_CACHE.get(cache_key)
    if hit and now - hit[0] < _MANDI_CACHE_TTL:
        return hit[1]

    api_key = settings.data_gov_api_key
    if api_key:
        params = {
            "api-key": api_key, "format": "json", "limit": "8",
            "filters[commodity]": commodity.title(),
        }
        if state:
            params["filters[state]"] = state.title()
        if market:
            params["filters[market]"] = market.title()
        url = f"https://api.data.gov.in/resource/{_MANDI_RESOURCE}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                rows = (r.json() or {}).get("records") or []
            if rows:
                prices = [{
                    "market": x.get("market"), "district": x.get("district"),
                    "state": x.get("state"), "variety": x.get("variety"),
                    "arrival_date": x.get("arrival_date"),
                    "min_price_rs_per_quintal": x.get("min_price"),
                    "modal_price_rs_per_quintal": x.get("modal_price"),
                    "max_price_rs_per_quintal": x.get("max_price"),
                } for x in rows[:5]]
                result = {
                    "ok": True, "source": "Agmarknet (data.gov.in)",
                    "price_type": "live mandi price",
                    "commodity": commodity, "prices": prices,
                    "message": ("Quote the modal price as ₹X per quintal, name "
                                "the mandi and the arrival_date it was recorded "
                                "on, and note prices vary by market."),
                    "is_mock": False,
                }
                _MANDI_CACHE[cache_key] = (now, result)
                return result
        except Exception as e:  # noqa: BLE001 — fall through to MSP
            import logging
            logging.getLogger("tools.mandi").warning(
                "Agmarknet fetch failed (%s); falling back to MSP", e)

    msp = _MSP_2025_26.get(commodity.lower())
    if msp:
        return {
            "ok": True, "source": "Government MSP table 2025-26",
            "price_type": "minimum support price (NOT today's mandi rate)",
            "commodity": commodity, "msp_rs_per_quintal": msp,
            "message": ("Live mandi rates are unavailable right now. Give the "
                        "MSP and say clearly it is the government support "
                        "price, not today's market rate; suggest the eNAM "
                        "portal or the local mandi for the live rate."),
            "is_mock": False,
        }
    return {"ok": False, "error": "no_data",
            "message": ("No live or MSP data for this commodity. Be honest "
                        "that you can't quote a price right now and point the "
                        "citizen to enam.gov.in or their local mandi.")}


# --- Scheme tools ----------------------------------------------------------

async def _schemes_search(args: dict, citizen_id: str) -> dict:
    from . import schemes
    ctx = _citizen_ctx(citizen_id)
    results = schemes.search(args.get("query", ""),
                             state_code=ctx["state_code"],
                             family=args.get("family", ""))
    return {"ok": True, "count": len(results),
            "schemes": [{"scheme_id": s["scheme_id"], "name": s["name"],
                         "family": s["family"], "benefit": s.get("benefit"),
                         "summary": s.get("summary"),
                         "helpline": s.get("helpline")} for s in results]}


async def _schemes_check_eligibility(args: dict, citizen_id: str) -> dict:
    from . import schemes
    from .store import store
    profile = dict((store.get_citizen(citizen_id) or {}).get("profile", {}))
    for k, v in (args.get("profile") or {}).items():
        profile[k] = v
    profile.setdefault("state_code", _citizen_ctx(citizen_id)["state_code"])
    return {"ok": True, **schemes.check_eligibility(args.get("scheme_id", ""), profile)}


async def _schemes_apply(args: dict, citizen_id: str) -> dict:
    from . import schemes
    from .records import service as rsvc
    s = schemes.get(args.get("scheme_id", ""))
    if not s:
        return {"ok": False, "error": "unknown_scheme"}
    ctx = _citizen_ctx(citizen_id)
    rec = rsvc.create_record(
        kind="scheme_application", citizen_id=citizen_id, msisdn=ctx["msisdn"],
        state_code=ctx["state_code"], department_id=s.get("owning_department", "social"),
        category="scheme.application", title=f"Application: {s['name']}",
        description=f"Application to {s['name']} ({s['scheme_id']}).",
        channel=args.get("_channel", "simulator"), lang=ctx["lang"],
        scheme_id=s["scheme_id"], initial_status="SUBMITTED",
        extra={"scheme_name": s["name"], "documents_required": s.get("documents_required", [])},
    )
    return {"ok": True, "record_id": rec.record_id, "status": rec.status,
            "scheme": s["name"], "documents_required": s.get("documents_required", []),
            "track_at": f"/api/v1/track/{rec.record_id}"}


# --- Project tools ---------------------------------------------------------

async def _projects_find(args: dict, citizen_id: str) -> dict:
    from . import projects
    ctx = _citizen_ctx(citizen_id)
    res = projects.find(state_code=args.get("state_code") or ctx["state_code"],
                        district=args.get("district", ""),
                        ptype=args.get("type", ""), query=args.get("query", ""))
    return {"ok": True, "count": len(res),
            "projects": [projects.summary(p["project_id"]) for p in res]}


async def _projects_track(args: dict, citizen_id: str) -> dict:
    from . import projects
    s = projects.summary(args.get("project_id", ""))
    if not s:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "project": s}


async def _projects_report_issue(args: dict, citizen_id: str) -> dict:
    from . import projects
    proj = projects.get(args.get("project_id", ""))
    a = dict(args)
    a["project_id"] = args.get("project_id")
    a["department_id"] = (proj or {}).get("department", "pwd")
    a["category"] = args.get("category", "road_defect")
    a.setdefault("title", f"Issue with project {args.get('project_id','')}")
    return _create_record_from_args(a, citizen_id,
                                    department_id=a["department_id"],
                                    default_category="road_defect")


# ---------------------------------------------------------------------------
# Register tools
# ---------------------------------------------------------------------------

register(Tool(
    id="digilocker.fetch_patta",
    name="Fetch Patta from DigiLocker",
    description="Retrieve the citizen's Patta (land record certificate) from DigiLocker. Requires explicit consent.",
    connector="digilocker",
    requires_consent=True,
    consent_scope="PATTA_FETCH",
    input_schema={
        "type": "object",
        "properties": {
            "survey_no": {"type": "string",
                          "description": "Survey number of the land. Optional."},
        },
        "required": [],
    },
    allowed_agents=["revenue", "agriculture", "cmo"],
    execute=_digilocker_fetch_patta,
))

register(Tool(
    id="digilocker.fetch_ec",
    name="Fetch Encumbrance Certificate from DigiLocker",
    description="Retrieve the citizen's Encumbrance Certificate (EC) for a property. Requires explicit consent.",
    connector="digilocker",
    requires_consent=True,
    consent_scope="EC_FETCH",
    input_schema={
        "type": "object",
        "properties": {"property_id": {"type": "string"}},
        "required": [],
    },
    allowed_agents=["revenue", "cmo"],
    execute=_digilocker_fetch_ec,
))

register(Tool(
    id="digilocker.fetch_dl",
    name="Fetch Driving Licence from DigiLocker",
    description="Retrieve the citizen's Driving Licence from DigiLocker. Requires explicit consent.",
    connector="digilocker",
    requires_consent=True,
    consent_scope="DL_FETCH",
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_agents=["transport", "cmo"],
    execute=_digilocker_fetch_dl,
))

register(Tool(
    id="digilocker.fetch_ration_card",
    name="Fetch Ration Card from DigiLocker",
    description="Retrieve the citizen's Ration Card details. Requires explicit consent.",
    connector="digilocker",
    requires_consent=True,
    consent_scope="RATION_FETCH",
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_agents=["ration", "cmo"],
    execute=_digilocker_fetch_ration,
))

register(Tool(
    id="water.register_complaint",
    name="Register Water Department Complaint",
    description="Register a complaint about water supply, leaks, sewerage etc.",
    connector="tnwater",
    requires_consent=False,
    consent_scope="",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string",
                         "enum": ["leak", "no_supply", "low_pressure", "sewer_blockage", "quality"]},
            "location": {"type": "string"},
            "details": {"type": "string"},
        },
        "required": ["category"],
    },
    allowed_agents=["water"],
    execute=_water_complaint_register,
))

register(Tool(
    id="cmo.create_grievance",
    name="Create a Public Grievance",
    description="File a public grievance with the Chief Minister's Cell.",
    connector="cmcell",
    requires_consent=False,
    consent_scope="",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "category": {"type": "string"},
        },
        "required": ["text"],
    },
    allowed_agents=["cmo"],
    execute=_grievance_create,
))


# ---------------------------------------------------------------------------
# Phase 6e — generic record / scheme / project tools
# ---------------------------------------------------------------------------

_ALL_AGENTS = ["cmo", "agriculture", "health", "ration", "revenue",
               "transport", "water", "housing", "wcd", "social", "pwd"]

register(Tool(
    id="agriculture.get_mandi_price",
    name="Get crop mandi price",
    description="Get today's mandi (market) price for a crop/commodity from Agmarknet, or the MSP as fallback. Use whenever a citizen asks for crop prices, mandi rates, bhav, or MSP.",
    connector="agmarknet", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "commodity": {"type": "string",
                      "description": "Crop name in English, e.g. Onion, Tomato, Paddy, Wheat"},
        "state": {"type": "string", "description": "Indian state name. Optional."},
        "market": {"type": "string", "description": "Mandi/market name. Optional."},
    }, "required": ["commodity"]},
    allowed_agents=["agriculture", "cmo"], execute=_mandi_price,
))

register(Tool(
    id="records.create",
    name="Register a trackable record",
    description="Create a trackable grievance / complaint / service request with a real reference number, SLA clock and L1-L4 escalation. Use when a citizen reports a problem the department must act on.",
    connector="records", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "department_id": {"type": "string"},
        "category": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "location": {"type": "string"},
        "priority": {"type": "string", "enum": ["normal", "high", "emergency"]},
    }, "required": ["category"]},
    allowed_agents=_ALL_AGENTS, execute=_records_create,
))

register(Tool(
    id="records.track",
    name="Track a record by reference number",
    description="Look up the live status, current desk/level and full timeline of a grievance/application by its reference number (e.g. GRV-TN-2026-000123).",
    connector="records", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "record_id": {"type": "string", "description": "Reference number"}},
        "required": ["record_id"]},
    allowed_agents=_ALL_AGENTS, execute=_records_track,
))

register(Tool(
    id="records.list_mine",
    name="List the citizen's records",
    description="List the citizen's own open and closed grievances, applications and service requests across departments.",
    connector="records", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "open_only": {"type": "boolean"}}, "required": []},
    allowed_agents=_ALL_AGENTS, execute=_records_list_mine,
))

register(Tool(
    id="records.send_reminder",
    name="Send a reminder on a record",
    description="Citizen nudge (Jansunwai-style): bump priority and ping the owning desk on an open record.",
    connector="records", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "record_id": {"type": "string"}}, "required": ["record_id"]},
    allowed_agents=_ALL_AGENTS, execute=_records_reminder,
))

register(Tool(
    id="records.submit_feedback",
    name="Submit feedback on a resolved record",
    description="Rate a resolved record 1-5. >=4 closes it; <=2 reopens it for fresh review (CM-Helpline satisfaction loop).",
    connector="records", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "record_id": {"type": "string"},
        "rating": {"type": "integer"},
        "comment": {"type": "string"}}, "required": ["record_id", "rating"]},
    allowed_agents=_ALL_AGENTS, execute=_records_feedback,
))

register(Tool(
    id="schemes.search",
    name="Search welfare schemes",
    description="Find welfare schemes by life-situation or keyword (housing, women, child, senior-citizen, farmer, health), scoped to the citizen's state.",
    connector="schemes", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "query": {"type": "string"},
        "family": {"type": "string", "enum": ["housing", "women_welfare",
                   "child_welfare", "senior_citizen", "farmer", "health"]}},
        "required": []},
    allowed_agents=_ALL_AGENTS, execute=_schemes_search,
))

register(Tool(
    id="schemes.check_eligibility",
    name="Check scheme eligibility",
    description="Explain whether a citizen qualifies for a scheme, rule by rule, using their profile.",
    connector="schemes", requires_consent=True, consent_scope="PROFILE_READ",
    input_schema={"type": "object", "properties": {
        "scheme_id": {"type": "string"},
        "profile": {"type": "object"}}, "required": ["scheme_id"]},
    allowed_agents=_ALL_AGENTS, execute=_schemes_check_eligibility,
))

register(Tool(
    id="schemes.apply",
    name="Apply to a welfare scheme",
    description="Create a trackable application record for a scheme and list the documents required.",
    connector="schemes", requires_consent=True, consent_scope="SCHEME_APPLY",
    input_schema={"type": "object", "properties": {
        "scheme_id": {"type": "string"}}, "required": ["scheme_id"]},
    allowed_agents=_ALL_AGENTS, execute=_schemes_apply,
))

register(Tool(
    id="projects.find_near_me",
    name="Find development projects nearby",
    description="List roads / buildings / water-works projects in the citizen's district or ward, with progress.",
    connector="projects", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "district": {"type": "string"}, "type": {"type": "string"},
        "query": {"type": "string"}}, "required": []},
    allowed_agents=_ALL_AGENTS, execute=_projects_find,
))

register(Tool(
    id="projects.track",
    name="Track a development project",
    description="Show milestones, percent complete, contractor, sanctioned cost and expected completion for a project id.",
    connector="projects", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}}, "required": ["project_id"]},
    allowed_agents=_ALL_AGENTS, execute=_projects_track,
))

register(Tool(
    id="projects.report_issue",
    name="Report an issue with a project",
    description="File a grievance linked to a development project (e.g. abandoned road work). Creates a trackable PWD record.",
    connector="projects", requires_consent=False, consent_scope="",
    input_schema={"type": "object", "properties": {
        "project_id": {"type": "string"}, "description": {"type": "string"},
        "location": {"type": "string"}}, "required": []},
    allowed_agents=_ALL_AGENTS, execute=_projects_report_issue,
))
