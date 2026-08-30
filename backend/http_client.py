"""Shared HTTP client config for Sarvam-bound calls.

Centralises the SSL trust-store decision in one place so every module
that talks to Sarvam (voice, vision, llm, diagnostics) handles
certificate verification consistently.

The problem: on Windows, Python's default SSL trust comes from `certifi`,
which doesn't include corporate-CA certificates injected by enterprise
proxies (Zscaler, Netskope, Bluecoat, etc.). The result is
`CERTIFICATE_VERIFY_FAILED` on every Sarvam call even though the cert is
trusted by the OS.

Three resolution paths, tried in order:
  1. truststore — uses the OS cert store (Windows, macOS Keychain, Linux CA).
     This automatically picks up corporate CAs. Best fix.
  2. SARVAM_CA_BUNDLE — explicit path to a .pem bundle the user provides.
     Useful when the corporate IT team gives you the chain.
  3. SARVAM_VERIFY_SSL=false — last resort, disables verification entirely.
     Logs a loud warning. Don't use in production.
"""
from __future__ import annotations

import logging
import os
import ssl

from .config import settings

log = logging.getLogger("sarvam.http")


_ssl_strategy: str = "unknown"
_truststore_injected: bool = False


def httpx_client_kwargs() -> dict:
    """Return kwargs to pass into httpx.AsyncClient(...) for Sarvam calls."""
    global _ssl_strategy

    # Option 3 — explicit opt-out (logged loudly so it's hard to leave on)
    if not getattr(settings, "sarvam_verify_ssl", True):
        if _ssl_strategy != "verify_off":
            log.warning(
                "SARVAM_VERIFY_SSL=false — TLS verification is DISABLED. "
                "Acceptable for local debugging only. Re-enable for production."
            )
            _ssl_strategy = "verify_off"
        return {"verify": False}

    # Option 2 — explicit CA bundle (corporate IT often hands you one)
    bundle = getattr(settings, "sarvam_ca_bundle", "")
    if bundle and os.path.isfile(bundle):
        if _ssl_strategy != "ca_bundle":
            log.info("Using explicit CA bundle: %s", bundle)
            _ssl_strategy = "ca_bundle"
        return {"verify": bundle}

    # Option 1 — truststore (uses OS cert store). Best for corporate networks.
    # We use inject_into_ssl() (a global monkeypatch of the stdlib `ssl` module)
    # rather than building an explicit `truststore.SSLContext`, because on
    # Python 3.14 that SSLContext subclass recurses infinitely in its
    # `verify_mode` property during the TLS handshake (RecursionError: maximum
    # recursion depth exceeded — breaks every Sarvam call). inject_into_ssl()
    # works correctly on 3.14; httpx then trusts the OS store via its default
    # verify=True. Injected once per process (idempotent guard).
    global _truststore_injected
    try:
        import truststore
        if not _truststore_injected:
            truststore.inject_into_ssl()
            _truststore_injected = True
        if _ssl_strategy != "truststore":
            log.info("Using OS cert store via truststore.inject_into_ssl "
                     "(handles corporate CAs).")
            _ssl_strategy = "truststore"
        return {}  # httpx uses the now-patched stdlib ssl (OS trust store)
    except ImportError:
        if _ssl_strategy != "certifi":
            log.info(
                "truststore not installed — using httpx default (certifi). "
                "If you hit SSL errors behind a corporate proxy, "
                "`pip install truststore` will likely fix them."
            )
            _ssl_strategy = "certifi"
        return {}  # httpx default = certifi


def current_strategy() -> str:
    """Return the SSL strategy actually in use (for diagnostics display)."""
    # Force a call so _ssl_strategy is populated
    httpx_client_kwargs()
    return _ssl_strategy
