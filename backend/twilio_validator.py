"""Twilio webhook signature validation.

Twilio signs every webhook with HMAC-SHA1 over (full URL + sorted form params).
Reference: https://www.twilio.com/docs/usage/security#validating-requests

We implement it inline so we don't depend on the official `twilio` Python
package (one less dependency). For x-www-form-urlencoded bodies the algorithm:

  signature = HMAC-SHA1(auth_token, full_url + concat(k+v for k,v in sorted(params)))
  signature = base64(signature)

In mock mode (no TWILIO_AUTH_TOKEN configured) or with
TWILIO_VALIDATE_SIGNATURES=false set, validation is bypassed and a warning
is logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Mapping

from .config import settings

log = logging.getLogger("twilio.validator")


def expected_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    """Compute the Twilio-style HMAC-SHA1 signature."""
    s = url
    for k in sorted(params.keys()):
        s += k + str(params[k])
    digest = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_request(*, signature_header: str | None, url: str,
                     params: Mapping[str, str]) -> bool:
    """Return True if the request is from Twilio (or validation is disabled)."""
    if settings.twilio_mock_mode:
        return True  # No Twilio configured — accept anything in mock mode
    if not settings.twilio_validate_signatures:
        log.warning("Twilio signature validation DISABLED via env var. "
                    "Do NOT do this in production.")
        return True
    if not signature_header:
        return False
    expected = expected_signature(url, params, settings.twilio_auth_token)
    return hmac.compare_digest(expected, signature_header)
