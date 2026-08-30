"""Lightweight citizen sessions + admin gate — Phase 6e.

Backward-compatible by design:
  - `auth/init` now issues a *signed* session token (HMAC-SHA256 over
    citizen_id + expiry). Previously the `wsToken` was random and never
    checked; now it actually proves the holder was issued a session for
    that citizen.
  - Enforcement is OPT-IN via REQUIRE_AUTH=true so existing dev flows keep
    working. Even with enforcement off, record-ownership checks (in the
    routes) still prevent the worst IDOR by requiring the caller to supply
    the owning citizen_id.
  - Admin routes can be gated by setting ADMIN_API_TOKEN; if unset, admin
    stays open (dev) but logs a warning at startup.

This is deliberately small and dependency-free (HMAC, not full OAuth) — the
production story is OIDC/SAML per the Phase-7 roadmap. The point here is to
close the "anyone can impersonate anyone / act on any record" hole.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException

from .config import settings

log = logging.getLogger("auth")

_SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
_REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or settings.is_production
_ADMIN_TOKEN = os.getenv("ADMIN_API_TOKEN", "")

_secret: Optional[bytes] = None


def _load_secret() -> bytes:
    """Load (or create) the HMAC signing secret. Persisted in the data dir
    so tokens survive a restart; if the dir is read-only we fall back to a
    process-ephemeral secret (tokens then only last for this process)."""
    global _secret
    if _secret is not None:
        return _secret
    p = Path(settings.data_dir) / "session_secret.key"
    try:
        if p.exists():
            _secret = p.read_bytes().strip()
            if _secret:
                return _secret
        _secret = secrets.token_bytes(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_secret)
    except Exception as e:
        log.warning("session secret persistence unavailable (%s); using ephemeral", e)
        if not _secret:
            _secret = secrets.token_bytes(32)
    return _secret


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_session(citizen_id: str, ttl: int = _SESSION_TTL) -> str:
    """token = base64(citizen_id|exp).base64(hmac)."""
    exp = int(time.time()) + ttl
    body = f"{citizen_id}|{exp}".encode()
    sig = hmac.new(_load_secret(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_session(token: str) -> Optional[str]:
    """Return the citizen_id if the token is valid + unexpired, else None."""
    if not token:
        return None
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _ub64(body_b64)
        expected = hmac.new(_load_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _ub64(sig_b64)):
            return None
        cid, exp = body.decode().rsplit("|", 1)
        if int(exp) < int(time.time()):
            return None
        return cid
    except Exception:
        return None


def require_auth_enabled() -> bool:
    return _REQUIRE_AUTH


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def citizen_from_token(authorization: str = Header(default="")) -> Optional[str]:
    """Extract + verify the citizen from a Bearer token. Returns the
    citizen_id, or None when no/invalid token AND enforcement is off.
    Raises 401 only when REQUIRE_AUTH is on and the token is missing/invalid."""
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    cid = verify_session(token) if token else None
    if _REQUIRE_AUTH and not cid:
        raise HTTPException(401, "valid session token required")
    return cid


def assert_owns(token_citizen: Optional[str], claimed_citizen: str) -> None:
    """Ownership guard for citizen self-service.
      - If a verified token is present, it MUST match the claimed citizen.
      - If enforcement is on, a token is required (handled upstream).
      - With enforcement off and no token, we allow (dev) but the caller
        still had to supply the owning citizen_id, which closes blind IDOR
        by reference-number alone."""
    if token_citizen and token_citizen != claimed_citizen:
        raise HTTPException(403, "token does not match this citizen")


def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Gate admin mutations. If ADMIN_API_TOKEN is configured, require it;
    otherwise allow (dev) — startup logs a warning in main.py."""
    if _ADMIN_TOKEN:
        if not hmac.compare_digest(x_admin_token or "", _ADMIN_TOKEN):
            raise HTTPException(401, "admin token required")


def admin_gate_configured() -> bool:
    return bool(_ADMIN_TOKEN)
