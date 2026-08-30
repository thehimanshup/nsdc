"""Demo authentication — signed role tokens with jurisdiction claims (ST-601).

PoC-grade, stdlib-only token: HMAC-SHA256 over a canonical JSON payload
(same construction as a JWT HS256, without the header ceremony). Swap for
Keycloak/OIDC at delivery — the CLAIMS SHAPE is the contract that persists:

    {sub, name, role, jurisdiction: {district?, state?, scheme?}, exp}

Security invariants:
  - role/jurisdiction come ONLY from the verified token, never from the
    request body (closes the trust-the-client hole).
  - tampered or expired tokens are rejected.
  - secret comes from SUBSTRATE_AUTH_SECRET or is generated per-run
    (dev convenience; persisted under data/ so restarts keep sessions).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schemas import Role

TOKEN_TTL_S = 8 * 3600

# Demo user directory — passwords are demo-only, printed in the runbook.
DEMO_USERS = {
    "meena":  {"password": "learner-demo", "name": "Meena (Learner)",
               "role": "learner", "jurisdiction": {}},
    "rajesh": {"password": "officer-demo", "name": "Rajesh (Scheme Manager)",
               "role": "officer",
               "jurisdiction": {"district": "South Delhi", "state": "Delhi",
                                "scheme": "pmkvy4"}},
    "leela":  {"password": "officer-demo-2", "name": "Leela (Scheme Manager)",
               "role": "officer",
               "jurisdiction": {"district": "North West Delhi", "state": "Delhi",
                                "scheme": "pmkvy4"}},
    "iyer":   {"password": "sme-demo", "name": "Dr. Iyer (Content SME)",
               "role": "sme", "jurisdiction": {}},
    "admin":  {"password": "admin-demo", "name": "Platform Admin",
               "role": "admin", "jurisdiction": {}},
}


def _secret() -> bytes:
    env = os.getenv("SUBSTRATE_AUTH_SECRET")
    if env:
        return env.encode()
    p = Path(os.getenv("DATA_DIR", "data")) / "substrate_auth_secret.key"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32))
    return p.read_text().strip().encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Principal:
    sub: str
    name: str
    role: Role
    jurisdiction: dict = field(default_factory=dict)

    @property
    def district(self) -> Optional[str]:
        return self.jurisdiction.get("district")


class AuthError(Exception):
    pass


def issue_token(username: str, password: str) -> str:
    user = DEMO_USERS.get(username)
    if not user or not hmac.compare_digest(user["password"], password):
        raise AuthError("invalid credentials")
    payload = {"sub": username, "name": user["name"], "role": user["role"],
               "jurisdiction": user["jurisdiction"],
               "exp": int(time.time()) + TOKEN_TTL_S}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_token(token: str) -> Principal:
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _unb64(body_b64)
        expected = hmac.new(_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig_b64)):
            raise AuthError("bad signature")
        payload = json.loads(body)
        if payload.get("exp", 0) < time.time():
            raise AuthError("token expired")
        return Principal(sub=payload["sub"], name=payload.get("name", ""),
                         role=Role(payload["role"]),
                         jurisdiction=payload.get("jurisdiction", {}))
    except AuthError:
        raise
    except Exception as e:
        raise AuthError(f"malformed token: {e}") from e


def principal_from_header(authorization: Optional[str]) -> Principal:
    """FastAPI dependency helper. `Bearer <token>` required."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    return verify_token(authorization.split(" ", 1)[1].strip())
