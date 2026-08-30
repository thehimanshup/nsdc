"""Cryptographic primitives for the consent ledger + audit log.

Phase 6 uses Ed25519 with on-disk keys. Phase 7 swaps in a CCA-licensed
HSM for production use.

If the `cryptography` package isn't installed, we fall back to HMAC-SHA256
with a server secret. Same security properties for our use case (server
is both signer and verifier in Phase 6) but less impressive on a slide deck.
The interface is the same — callers shouldn't care which backend is active.

Key files:
  data/keys/server_ed25519.pem        Server signing key
  data/keys/server_ed25519.pub        Server public key
  data/keys/server_hmac.key           Server HMAC secret (fallback only)

Threats covered:
  - Tampering with a consent grant on disk (signature won't verify)
  - Re-ordering ledger entries (prevHash chain breaks)
  - Forging a grant retroactively (no valid signature without server key)

Threats NOT covered (Phase 7):
  - Citizen impersonation (requires citizen device key — Phase 7)
  - Key compromise + rewriting entire chain (needs transparency log — Phase 7)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("crypto")


KEYS_DIR = Path(settings.data_dir) / "keys"
ED25519_PRIV = KEYS_DIR / "server_ed25519.pem"
ED25519_PUB  = KEYS_DIR / "server_ed25519.pub"
HMAC_KEY     = KEYS_DIR / "server_hmac.key"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_backend: str = "unknown"
_ed25519_private = None
_ed25519_public_b64: str = ""
_hmac_secret: bytes = b""


def _try_init_ed25519() -> bool:
    """Try to load (or create) an Ed25519 keypair. Returns True on success."""
    global _ed25519_private, _ed25519_public_b64
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, PublicFormat, NoEncryption,
            load_pem_private_key,
        )
        import base64
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        if ED25519_PRIV.exists():
            with open(ED25519_PRIV, "rb") as f:
                _ed25519_private = load_pem_private_key(f.read(), password=None)
            log.info("Loaded existing Ed25519 server key from %s", ED25519_PRIV)
        else:
            _ed25519_private = Ed25519PrivateKey.generate()
            pem = _ed25519_private.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
            )
            ED25519_PRIV.write_bytes(pem)
            try:
                os.chmod(ED25519_PRIV, 0o600)
            except Exception:
                pass
            log.info("Generated new Ed25519 server key at %s", ED25519_PRIV)
        # Public key (raw, base64-encoded for compact storage in entries)
        pub_raw = _ed25519_private.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw,
        )
        _ed25519_public_b64 = base64.b64encode(pub_raw).decode()
        ED25519_PUB.write_text(_ed25519_public_b64)
        return True
    except ImportError:
        return False
    except Exception as e:
        log.warning("Ed25519 init failed: %s. Falling back to HMAC.", e)
        return False


def _init_hmac_fallback() -> None:
    """Initialise the HMAC fallback (no extra dependency)."""
    global _hmac_secret
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if HMAC_KEY.exists():
        _hmac_secret = HMAC_KEY.read_bytes().strip()
        log.info("Loaded HMAC secret from %s", HMAC_KEY)
    else:
        _hmac_secret = secrets.token_bytes(32)
        HMAC_KEY.write_bytes(_hmac_secret)
        try:
            os.chmod(HMAC_KEY, 0o600)
        except Exception:
            pass
        log.info("Generated new HMAC secret at %s", HMAC_KEY)


def init_keys() -> None:
    """Set up the crypto backend. Called once at server startup."""
    global _backend
    if _try_init_ed25519():
        _backend = "ed25519"
    else:
        _init_hmac_fallback()
        _backend = "hmac"
    log.info("Crypto backend: %s", _backend)


def backend_info() -> dict:
    return {
        "backend": _backend,
        "public_key": _ed25519_public_b64 if _backend == "ed25519" else "",
        "algorithm": "ed25519" if _backend == "ed25519" else "hmac-sha256",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: dict) -> bytes:
    """Deterministic JSON bytes for hashing. Sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_bytes(data: bytes) -> str:
    """Sign with the active backend. Returns base64 signature."""
    import base64
    if _backend == "ed25519" and _ed25519_private is not None:
        sig = _ed25519_private.sign(data)
        return "ed25519:" + base64.b64encode(sig).decode()
    elif _backend == "hmac":
        mac = hmac.new(_hmac_secret, data, hashlib.sha256).digest()
        return "hmac:" + base64.b64encode(mac).decode()
    else:
        raise RuntimeError("Crypto backend not initialised. Call init_keys() first.")


def verify_signature(data: bytes, signature: str) -> bool:
    """Verify a signature produced by sign_bytes()."""
    import base64
    try:
        scheme, _, sig_b64 = signature.partition(":")
        sig = base64.b64decode(sig_b64)
        if scheme == "ed25519":
            if _ed25519_private is None:
                return False
            _ed25519_private.public_key().verify(sig, data)
            return True
        if scheme == "hmac":
            if not _hmac_secret:
                return False
            expected = hmac.new(_hmac_secret, data, hashlib.sha256).digest()
            return hmac.compare_digest(sig, expected)
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Merkle tree (for daily audit-log roots)
# ---------------------------------------------------------------------------

def merkle_root(leaves: list[str]) -> str:
    """Compute a SHA-256 Merkle root over a list of hex leaf hashes."""
    if not leaves:
        return sha256_hex(b"")
    level = [bytes.fromhex(h) for h in leaves]
    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left  # duplicate last
            next_level.append(hashlib.sha256(left + right).digest())
        level = next_level
    return level[0].hex()
