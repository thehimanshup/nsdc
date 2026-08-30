"""MCP client loader — connect to EXTERNAL tool servers.

Unlike the Python plugins in `tool_plugins/` (whose code lives inside this
project), an MCP server's code lives on a *separate* machine owned by someone
else. We are only the **client**: we connect over the network, ask "what tools
do you have?", and forward calls to it. We never host that code.

Each discovered remote tool is wrapped as a normal local `Tool` whose
`execute()` forwards the call across the network and returns the server's
reply. Once wrapped it is registered into the same `_TOOLS` registry as every
other tool, so on the admin Tools page it behaves identically — same enable
toggle, agent wiring, Test button and consent/audit path — except for the
`mcp:<server>` source/connector badge.

Config — data/mcp_servers.json — is just *addresses*, never code::

    {
      "land_records": {
        "url": "https://landrecords.example.gov.in/mcp",
        "transport": "streamable_http",      // or "sse"; default streamable_http
        "auth_token_env": "LAND_RECORDS_MCP_TOKEN",
        "enabled": true
      }
    }

Resilience: the official `mcp` SDK is an OPTIONAL dependency. If it's not
installed, or `data/mcp_servers.json` is absent, or a server is unreachable,
this loader logs and degrades to "no MCP tools" — it never breaks startup or
the other tools (same failure-isolation as the Phase 6e loaders).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse

from .config import settings
from .tools import Tool, register, _TOOLS

log = logging.getLogger("tools.mcp")

_LOCK = RLock()
_SERVERS: dict[str, dict] = {}          # name -> validated config
_REGISTERED_IDS: set[str] = set()       # mcp tool ids we registered (for reload cleanup)
_STATUS: dict[str, dict] = {}           # name -> {connected, tools, error} (last connect_all)

# Per-connection timeout. We use SHORT-LIVED connections (open → use → close)
# rather than long-lived pooled sessions: a persistent session opened during
# lifespan startup and closed during shutdown trips anyio's "cancel scope
# exited in a different task" error, and a hung connect must never block
# startup. wait_for runs each connection in its own task so enter+exit happen
# in the same task and a dead server fails fast instead of hanging.
_CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "10"))


def _path() -> Path:
    p = Path(settings.data_dir) / "mcp_servers.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Egress governance (§7) — India-only data path
# ---------------------------------------------------------------------------

def _allowed_hosts() -> set[str]:
    raw = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _egress_ok(url: str) -> tuple[bool, str]:
    """Enforce an optional egress allow-list and flag overseas hosts.

    - If MCP_ALLOWED_HOSTS is set, the host MUST be on it (hard block).
    - Otherwise allow, but warn for hosts that aren't obviously India-based
      (.in / .gov.in / .nic.in) — the data-residency rule in the design.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False, "no host in url"
    allow = _allowed_hosts()
    if allow and host not in allow:
        return False, f"host {host} not in MCP_ALLOWED_HOSTS"
    india = host.endswith(".in") or host.endswith(".gov.in") or host.endswith(".nic.in")
    if not india and not allow:
        log.warning("MCP server %s appears OVERSEAS (data-residency risk). "
                    "Set MCP_ALLOWED_HOSTS to govern egress explicitly.", host)
    return True, ""


# ---------------------------------------------------------------------------
# Config load (sync)
# ---------------------------------------------------------------------------

def load() -> int:
    """Read data/mcp_servers.json into the config cache. Returns server count.

    Network connection happens later in `connect_all()` (needs the event loop).
    A missing/corrupt file is treated as "no servers"."""
    path = _path()
    with _LOCK:
        _SERVERS.clear()
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception as e:
            log.error("Failed to read %s: %s — no MCP servers loaded", path, e)
            return 0
        if not isinstance(raw, dict):
            log.error("mcp_servers.json must be an object keyed by server name")
            return 0
        for name, cfg in raw.items():
            if not isinstance(cfg, dict) or not cfg.get("url"):
                log.warning("Skipping MCP server '%s': missing url", name)
                continue
            # Optional: per-tool keyword triggers so a remote tool is reachable
            # from the keyword-matching chat path. { "tool_name": ["regex", ...] }
            kw = cfg.get("tool_keywords") or {}
            _SERVERS[name] = {
                "url": str(cfg["url"]),
                "transport": str(cfg.get("transport", "streamable_http")),
                "auth_token_env": cfg.get("auth_token_env", ""),
                "enabled": bool(cfg.get("enabled", True)),
                "tool_keywords": kw if isinstance(kw, dict) else {},
            }
    log.info("Configured %d MCP server(s) from %s", len(_SERVERS), path)
    return len(_SERVERS)


# ---------------------------------------------------------------------------
# Connect + wrap (async)
# ---------------------------------------------------------------------------

def _headers_for(cfg: dict) -> dict:
    env = cfg.get("auth_token_env")
    token = os.getenv(env, "") if env else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _normalise_result(res: object) -> dict:
    """Turn an mcp CallToolResult into our plain-dict tool result shape."""
    # Prefer structured content if the server provided it.
    structured = getattr(res, "structuredContent", None)
    if isinstance(structured, dict):
        out = dict(structured)
        out.setdefault("ok", not getattr(res, "isError", False))
        return out
    # Otherwise join any text content blocks.
    texts = []
    for block in (getattr(res, "content", None) or []):
        t = getattr(block, "text", None)
        if t:
            texts.append(t)
    joined = "\n".join(texts) if texts else None
    # If the server returned a single JSON object as text, surface it as a real
    # dict so MCP tools behave like native ones (clean Test output, structured
    # data to the agent) instead of a JSON-encoded string.
    if joined:
        try:
            parsed = json.loads(joined)
            if isinstance(parsed, dict):
                parsed.setdefault("ok", not getattr(res, "isError", False))
                return parsed
        except Exception:
            pass
    return {
        "ok": not getattr(res, "isError", False),
        "result": joined,
    }


def _make_execute(server_name: str, remote_tool_name: str, cfg: dict):
    async def _execute(args: dict, citizen_id: str) -> dict:
        try:
            res = await _with_session(
                cfg, lambda s: s.call_tool(remote_tool_name, arguments=args or {}))
            return _normalise_result(res)
        except (asyncio.TimeoutError, TimeoutError):
            return {"ok": False, "error": "mcp_unavailable",
                    "message": (f"The '{server_name}' service didn't respond in "
                                f"time. Please try again shortly.")}
        except Exception as e:  # noqa: BLE001
            log.warning("MCP call %s/%s failed: %s", server_name, remote_tool_name, e)
            return {"ok": False, "error": "mcp_call_failed", "message": str(e)}
    return _execute


async def _open_session(stack: AsyncExitStack, cfg: dict):
    """Open an MCP ClientSession inside `stack`. Returns the initialized session.
    The stack MUST be entered and exited in the same task (see _with_session)."""
    from mcp import ClientSession  # imported lazily — optional dependency

    transport = cfg["transport"]
    headers = _headers_for(cfg)
    if transport == "sse":
        from mcp.client.sse import sse_client
        read, write = await stack.enter_async_context(
            sse_client(cfg["url"], headers=headers))
    else:
        from mcp.client.streamable_http import streamablehttp_client
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(cfg["url"], headers=headers))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def _err_str(e: BaseException) -> str:
    """Flatten an anyio/asyncio ExceptionGroup to the most useful leaf message
    (a bare 'unhandled errors in a TaskGroup' is useless in the admin UI)."""
    inner = getattr(e, "exceptions", None)
    if inner:
        return _err_str(inner[0])
    return str(e) or e.__class__.__name__


async def _with_session(cfg: dict, fn):
    """Open a short-lived session, run ``await fn(session)``, then close it —
    all within one task and bounded by _CONNECT_TIMEOUT.

    asyncio.wait_for runs the inner coroutine in its own task, so the session's
    AsyncExitStack is entered and exited in the SAME task (avoiding anyio's
    cross-task cancel-scope error), and a hung/unreachable server raises
    TimeoutError instead of blocking forever."""
    async def _run():
        async with AsyncExitStack() as stack:
            session = await _open_session(stack, cfg)
            return await fn(session)
    return await asyncio.wait_for(_run(), timeout=_CONNECT_TIMEOUT)


async def connect_all() -> int:
    """(Re)discover every configured+enabled server's tools and register them.

    Uses a short-lived connection per server for discovery; the wrapper tools
    open their own short-lived connection per call. Returns tools registered.
    Drops previously-registered MCP tools first so this doubles as reload."""
    await aclose()  # drop previously-registered MCP wrapper tools

    if not _SERVERS:
        return 0
    try:
        import mcp  # noqa: F401 — presence check only
    except Exception:
        log.warning("MCP servers are configured but the 'mcp' SDK is not "
                    "installed. Run `pip install mcp` to enable them. "
                    "Skipping — all other tools are unaffected.")
        return 0

    registered = 0
    _STATUS.clear()
    for name, cfg in list(_SERVERS.items()):
        if not cfg.get("enabled", True):
            _STATUS[name] = {"connected": False, "tools": [], "error": "disabled"}
            continue
        ok, why = _egress_ok(cfg["url"])
        if not ok:
            log.error("MCP server '%s' blocked by egress policy: %s", name, why)
            _STATUS[name] = {"connected": False, "tools": [], "error": f"blocked: {why}"}
            continue
        try:
            listing = await _with_session(cfg, lambda s: s.list_tools())
        except (asyncio.TimeoutError, TimeoutError):
            log.error("MCP server '%s' timed out after %ss (its tools will be "
                      "unavailable)", name, _CONNECT_TIMEOUT)
            _STATUS[name] = {"connected": False, "tools": [], "error": "timeout"}
            continue
        except Exception as e:  # noqa: BLE001 — one bad server must not kill the rest
            msg = _err_str(e)
            log.error("MCP server '%s' unreachable (its tools will be "
                      "unavailable): %s", name, msg)
            _STATUS[name] = {"connected": False, "tools": [], "error": msg}
            continue
        kw_map = cfg.get("tool_keywords") or {}
        ids = []
        for rt in listing.tools:
            tool_id = f"mcp.{name}.{rt.name}"
            register(Tool(
                id=tool_id,
                name=getattr(rt, "title", None) or rt.name,
                description=rt.description or f"{rt.name} (via MCP server {name})",
                connector=f"mcp:{name}",
                requires_consent=False,
                consent_scope="",
                input_schema=getattr(rt, "inputSchema", None)
                             or {"type": "object", "properties": {}, "required": []},
                allowed_agents=[],   # operator wires it on the Tools page
                execute=_make_execute(name, rt.name, cfg),
                category=f"mcp:{name}",
                source="mcp",
                trigger_patterns=list(kw_map.get(rt.name, [])),
            ))
            _REGISTERED_IDS.add(tool_id)
            ids.append(tool_id)
            registered += 1
        _STATUS[name] = {"connected": True, "tools": ids, "error": None}
        log.info("MCP server '%s': registered %d tool(s)", name, len(ids))
    return registered


async def aclose() -> None:
    """Drop all registered MCP wrapper tools. No persistent connections to close
    (each call/discovery uses its own short-lived session)."""
    with _LOCK:
        for tid in list(_REGISTERED_IDS):
            _TOOLS.pop(tid, None)
        _REGISTERED_IDS.clear()


def server_names() -> list[str]:
    with _LOCK:
        return sorted(_SERVERS.keys())


# ---------------------------------------------------------------------------
# Admin management — add/edit/delete servers from the UI + list with status
# ---------------------------------------------------------------------------

def _save_file_locked() -> None:
    """Atomic write of the current server config. Caller must hold _LOCK."""
    path = _path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_SERVERS, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def save_server(name: str, *, url: str, transport: str = "streamable_http",
                auth_token_env: str = "", enabled: bool = True,
                tool_keywords: dict | None = None) -> dict:
    """Insert/update one server in data/mcp_servers.json. Does NOT connect —
    the caller awaits connect_all() afterwards to (re)establish sessions."""
    with _LOCK:
        _SERVERS[name] = {
            "url": str(url),
            "transport": str(transport or "streamable_http"),
            "auth_token_env": auth_token_env or "",
            "enabled": bool(enabled),
            "tool_keywords": tool_keywords if isinstance(tool_keywords, dict) else {},
        }
        _save_file_locked()
    log.info("Saved MCP server config: %s -> %s", name, url)
    return dict(_SERVERS[name])


def delete_server(name: str) -> bool:
    """Remove a server from config (caller reconnects to drop its tools)."""
    with _LOCK:
        if name not in _SERVERS:
            return False
        del _SERVERS[name]
        _STATUS.pop(name, None)
        _save_file_locked()
    log.info("Deleted MCP server config: %s", name)
    return True


def list_servers() -> list[dict]:
    """Config + last-known connection status for every configured server."""
    with _LOCK:
        out = []
        for name, cfg in _SERVERS.items():
            st = _STATUS.get(name, {})
            out.append({
                "name": name,
                "url": cfg.get("url", ""),
                "transport": cfg.get("transport", "streamable_http"),
                "auth_token_env": cfg.get("auth_token_env", ""),
                "enabled": cfg.get("enabled", True),
                "tool_keywords": cfg.get("tool_keywords", {}),
                "connected": st.get("connected", False),
                "tools": st.get("tools", []),
                "error": st.get("error"),
            })
        return sorted(out, key=lambda s: s["name"])


async def probe(url: str, transport: str = "streamable_http",
                auth_token_env: str = "") -> dict:
    """Try to connect to a server WITHOUT persisting it, and list its tools.

    Powers the Tools-page "Test connection" button so an operator can verify an
    address before saving it. Opens a throwaway session and closes it."""
    try:
        import mcp  # noqa: F401
    except Exception:
        return {"ok": False, "error": "the 'mcp' SDK is not installed on the server"}
    ok, why = _egress_ok(url)
    if not ok:
        return {"ok": False, "error": f"blocked by egress policy: {why}"}
    cfg = {"url": url, "transport": transport or "streamable_http",
           "auth_token_env": auth_token_env or ""}
    try:
        listing = await _with_session(cfg, lambda s: s.list_tools())
        return {"ok": True, "count": len(listing.tools),
                "tools": [{"name": rt.name,
                           "description": (rt.description or "")[:160]}
                          for rt in listing.tools]}
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "error": f"timed out after {_CONNECT_TIMEOUT}s "
                                      f"(server unreachable or not responding)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _err_str(e)}
