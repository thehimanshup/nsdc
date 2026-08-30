"""Mandi-price MCP server — live daily crop/vegetable/fruit prices.

This is a STANDALONE external MCP server (like a third-party tool service). The
gov-services backend connects to it purely as a client over the network; it
never imports this code.

What it does
------------
Exposes one MCP tool, `mandi_price`, that returns today's market (mandi) price
for any commodity — vegetables, fruits, grains, pulses — from the Government of
India's official Agmarknet feed on data.gov.in. When the live API is
unavailable (no key / network / commodity not reported today) it falls back to
the published Minimum Support Price (MSP) and says so clearly.

Source
------
data.gov.in resource "Variety-wise Daily Market Prices of Commodities"
    https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070
Official, free. Needs a free API key:
    register at https://data.gov.in  (My Account -> Generate API key)
    then put it in the project's .env (this server auto-loads it):
        DATA_GOV_IN_API_KEY=your_key_here

Run it
------
    python mcp_servers/mandi_price_server.py

Serves the Model Context Protocol over streamable-HTTP at
    http://127.0.0.1:9002/mcp

Then on the admin Tools page -> MCP servers -> Add server:
    name      = mandi_prices
    url       = http://127.0.0.1:9002/mcp   (the /mcp path is required)
    transport = streamable_http
Save & connect, then wire the `mandi_price` tool to the agriculture / cmo
agents.

Requires (already in the project venv):  mcp, httpx, python-dotenv, truststore
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Flush prints per-line so the [mandi_price] call logs appear immediately in the
# console (and when stdout is redirected to a file), not stuck in a buffer.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

# Load the project's .env (one level up) so this standalone process uses the
# SAME DATA_GOV_IN_API_KEY the backend does — one source of truth. Without this
# the server would only see vars set in its own shell (or the demo fallback).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001 — dotenv optional; env / demo key still work
    pass

# Corporate networks often do TLS interception, so Python's default (certifi)
# cert bundle rejects api.data.gov.in with CERTIFICATE_VERIFY_FAILED. truststore
# makes ssl/httpx use the OS trust store, which DOES trust the corporate CA.
# (Same approach the backend uses for Sarvam — see .env notes.) Best-effort.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 — degrade to default certs if unavailable
    pass

# Port 9002 so it doesn't clash with the backend (:8000) or the demo server
# (:9001). Override with MANDI_PRICE_PORT if needed.
_PORT = int(os.getenv("MANDI_PRICE_PORT", "9002"))
mcp = FastMCP("mandi_price_tools", host="127.0.0.1", port=_PORT)

# data.gov.in Agmarknet daily mandi prices.
_RESOURCE = "9ef84268-d588-465a-a308-a864a43d0070"
# Corporate inline TLS-inspection appliances on some networks STALL the default
# "python-httpx" User-Agent (the request connects + handshakes but the response
# read hangs forever). A browser User-Agent + Connection: close sails through.
# Verified necessary on the HCL network — without this, live calls time out.
_HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "Connection": "close",
}
# data.gov.in's public SAMPLE key — works out of the box but is shared and
# rate-limited. Override with your own free key via DATA_GOV_IN_API_KEY (.env).
_DEMO_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
_API_KEY = os.getenv("DATA_GOV_IN_API_KEY", "").strip() or _DEMO_KEY
# Whether the key came from .env/env (your own) or the shared demo fallback.
_KEY_SOURCE = (".env / env var" if os.getenv("DATA_GOV_IN_API_KEY", "").strip()
               else "built-in DEMO key (shared, rate-limited)")


def _mask(k: str) -> str:
    """Show enough of the key to identify it in logs without leaking it."""
    return f"{k[:8]}…{k[-4:]}" if k and len(k) > 14 else "(none)"


# Simple in-process cache so repeat asks within 6h don't re-hit the API.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 6 * 3600.0

# Minimum Support Prices, Rs/quintal (2025-26 season, Govt. of India).
# FALLBACK ONLY — the live Agmarknet rate is always preferred. Note: MSP exists
# for grains/pulses/oilseeds, NOT for most vegetables & fruits.
_MSP_2025_26: dict[str, int] = {
    "paddy": 2369, "rice": 2369, "wheat": 2425, "maize": 2400,
    "jowar": 3699, "bajra": 2775, "ragi": 4886, "tur": 8000, "arhar": 8000,
    "moong": 8768, "urad": 7800, "groundnut": 7263, "soyabean": 5328,
    "soybean": 5328, "sunflower": 7721, "cotton": 7710, "sesamum": 9846,
    "nigerseed": 9537, "sugarcane": 355,
}


@mcp.tool()
def mandi_price(commodity: str, state: str = "", market: str = "") -> dict:
    """Get today's mandi (market) price for a crop, vegetable or fruit from the
    official Agmarknet feed (data.gov.in). Use whenever a citizen asks for crop
    prices, mandi rates, vegetable/fruit prices, 'bhav', or MSP.

    Args:
        commodity: Commodity name in English, e.g. "Onion", "Tomato", "Potato",
                   "Banana", "Apple", "Wheat", "Paddy". Required.
        state:     Indian state name to narrow results, e.g. "Tamil Nadu".
                   Optional.
        market:    Specific mandi/market name. Optional.
    """
    commodity = (commodity or "").strip()
    if not commodity:
        return {"ok": False, "error": "commodity_required",
                "message": "Which crop/vegetable/fruit do you want the price for?"}
    state = (state or "").strip()
    market = (market or "").strip()

    cache_key = f"{commodity.lower()}|{state.lower()}|{market.lower()}"
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    # ---- Live Agmarknet lookup --------------------------------------------
    if _API_KEY:
        params = {
            "api-key": _API_KEY, "format": "json", "limit": "8",
            "filters[commodity]": commodity.title(),
        }
        if state:
            params["filters[state]"] = state.title()
        if market:
            params["filters[market]"] = market.title()
        url = f"https://api.data.gov.in/resource/{_RESOURCE}"
        print(f"[mandi_price] -> GET data.gov.in  commodity={commodity.title()!r} "
              f"state={state or '-'}  key={_mask(_API_KEY)} ({_KEY_SOURCE})")
        try:
            # data.gov.in is often slow — generous read timeout + up to 3 tries.
            _timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
            rows, _status = None, None
            for _attempt in range(1, 4):
                try:
                    with httpx.Client(timeout=_timeout, headers=_HTTP_HEADERS) as client:
                        r = client.get(url, params=params)
                        r.raise_for_status()
                        rows = (r.json() or {}).get("records") or []
                        _status = r.status_code
                    break
                except (httpx.TimeoutException, httpx.TransportError) as _e:
                    print(f"[mandi_price]    attempt {_attempt}/3 failed: "
                          f"{type(_e).__name__} — retrying")
                    if _attempt == 3:
                        raise
            print(f"[mandi_price] <- HTTP {_status}, {len(rows or [])} record(s)")
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
                    "ok": True,
                    "source": "Agmarknet (data.gov.in) - via MCP",
                    "price_type": "live mandi price",
                    "commodity": commodity,
                    "prices": prices,
                    "message": ("Quote the modal price as Rs X per quintal, name "
                                "the mandi and the arrival_date it was recorded "
                                "on, and note prices vary by market."),
                    "is_mock": False,
                }
                _CACHE[cache_key] = (now, result)
                return result
            # Key present but no rows for this commodity/filter today.
            no_rows = {
                "ok": True,
                "source": "Agmarknet (data.gov.in) - via MCP",
                "price_type": "no live record today",
                "commodity": commodity,
                "prices": [],
                "message": (f"No mandi reported a price for '{commodity}'"
                            + (f" in {state}" if state else "")
                            + " today. Suggest checking enam.gov.in or the local "
                              "mandi, or try without the state filter."),
                "is_mock": False,
            }
            _CACHE[cache_key] = (now, no_rows)
            return no_rows
        except Exception as e:  # noqa: BLE001 — fall through to MSP
            print(f"[mandi_price] Agmarknet fetch failed: {e} — falling back to MSP")

    # ---- MSP fallback ------------------------------------------------------
    msp = _MSP_2025_26.get(commodity.lower())
    if msp:
        return {
            "ok": True,
            "source": "Government MSP table 2025-26 - via MCP",
            "price_type": "minimum support price (NOT today's mandi rate)",
            "commodity": commodity,
            "msp_rs_per_quintal": msp,
            "message": ("Live mandi rates are unavailable right now. Give the MSP "
                        "and say clearly it is the government support price, not "
                        "today's market rate; suggest eNAM or the local mandi for "
                        "the live rate."),
            "is_mock": False,
        }

    # No key and no MSP entry (typical for vegetables/fruits without a key).
    reason = ("No DATA_GOV_IN_API_KEY is set, so live prices are unavailable"
              if not _API_KEY else "No live or MSP data for this commodity")
    return {
        "ok": False, "error": "no_data",
        "commodity": commodity,
        "message": (f"{reason}. Be honest that you can't quote a price right now "
                    "and point the citizen to enam.gov.in or their local mandi. "
                    "(Set DATA_GOV_IN_API_KEY to enable live Agmarknet prices.)"),
    }


if __name__ == "__main__":
    print(f"Mandi-price MCP server on http://127.0.0.1:{_PORT}/mcp  (Ctrl+C to stop)")
    print(f"API key in use: {_mask(_API_KEY)}  (source: {_KEY_SOURCE})")
    print("Watch this window — every live call prints a [mandi_price] -> GET / "
          "<- HTTP line so you can see the key being used.")
    mcp.run(transport="streamable-http")
