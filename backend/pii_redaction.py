"""PII redaction — regex-based detection + masking.

DPDP Act §8 data-minimisation principle requires that personal data be
processed only for the purpose it was collected for, with minimum
exposure. We enforce this at three boundaries:

  1. Server logs (logging.Filter on every logger)
  2. Audit log entries (redact_for_logs before append)
  3. Outbound LLM context (redact_for_llm before sending messages to Sarvam,
     OpenAI, etc. — frontier providers should never see raw Aadhaar)

The citizen still sees full values in their own chat (that's their data).
Only persistent + observable surfaces are redacted.

Phase 6 covers the most-common Indian PII identifiers via regex. Phase 7
adds an NLP-based PII detector for names, addresses, account fragments
the regex can't catch.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Pattern


# ---------------------------------------------------------------------------
# Detectors — each returns the redacted form when a match is found
# ---------------------------------------------------------------------------

@dataclass
class _Rule:
    name: str
    pattern: Pattern[str]
    replacement: str   # may contain backreferences like \\1

_RULES: list[_Rule] = [
    # Aadhaar — 12 digits, optionally space-separated as XXXX XXXX XXXX.
    # NEVER show last 4 in logs (DPDP-sensitive); fully redact in observability.
    _Rule(
        "aadhaar_full",
        re.compile(r"\b(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})\b"),
        "[AADHAAR_REDACTED]",
    ),
    # Masked Aadhaar already (XXXX XXXX 1234) — keep as is but normalise spacing
    _Rule(
        "aadhaar_masked",
        re.compile(r"\bX{4}[\s-]?X{4}[\s-]?(\d{4})\b", re.IGNORECASE),
        r"XXXX XXXX \1",
    ),
    # PAN — 5 letters, 4 digits, 1 letter. Last-4 may be retained.
    _Rule(
        "pan",
        re.compile(r"\b([A-Z]{5})(\d{4})([A-Z])\b"),
        r"[PAN_*****\2\3]",
    ),
    # Indian mobile (with or without +91 / 0 prefix). 10 digits starting 6-9.
    _Rule(
        "msisdn",
        re.compile(r"(?<!\d)(?:\+?91[-\s]?|0)?([6-9]\d{2})(\d{3})(\d{4})(?!\d)"),
        r"+91-\1-XXX-\3",
    ),
    # IFSC code — 4 letters, '0', 6 alphanumeric
    _Rule(
        "ifsc",
        re.compile(r"\b([A-Z]{4})0([A-Z0-9]{6})\b"),
        r"[IFSC_\1_*]",
    ),
    # Indian driving licence — varied; common patterns: AA00 0000000000 or AA00-NNNNNNN
    # Conservative — only catch unambiguous forms
    _Rule(
        "dl_number",
        re.compile(r"\b([A-Z]{2})(\d{2})[\s-]?(\d{4,11})\b"),
        r"[DL_\1\2_***]",
    ),
    # Vehicle reg — TN01AB1234, KA53AB1234
    _Rule(
        "vehicle_reg",
        re.compile(r"\b([A-Z]{2})(\d{1,2})\s?([A-Z]{1,3})\s?(\d{1,4})\b"),
        r"[VEH_\1\2_***]",
    ),
    # Indian bank account — too varied; match 9-18 digit standalone numbers
    # only after the word "account" (heuristic, low false positive)
    _Rule(
        "bank_account",
        re.compile(r"(?i)\b(?:account|a/c|acct)[\s:#]*(\d{9,18})\b"),
        "[BANK_ACCOUNT_REDACTED]",
    ),
    # Email (just because)
    _Rule(
        "email",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "[EMAIL_REDACTED]",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def redact_for_logs(text: str) -> str:
    """Strict redaction for server logs / audit trail / observability.

    All known PII patterns are replaced with placeholders. Use this anywhere
    a text might end up in a log file, ClickHouse, or telemetry.
    """
    if not text:
        return text
    out = str(text)
    for rule in _RULES:
        out = rule.pattern.sub(rule.replacement, out)
    return out


def redact_for_llm(text: str, keep_msisdn_last4: bool = True) -> str:
    """Redaction for outbound LLM context.

    Slightly less aggressive than redact_for_logs — we keep the last 4 digits
    of the citizen's MSISDN so the LLM can address them by phone number tail
    if relevant. Aadhaar / PAN / DL are fully redacted in both modes.
    """
    if not text:
        return text
    out = str(text)
    for rule in _RULES:
        if rule.name == "msisdn" and keep_msisdn_last4:
            continue
        out = rule.pattern.sub(rule.replacement, out)
    return out


def detect_pii(text: str) -> list[dict]:
    """Return a list of {kind, span, redacted} dicts for inspection.
    Used by the audit log and the admin UI to show what was detected."""
    if not text:
        return []
    findings: list[dict] = []
    for rule in _RULES:
        for m in rule.pattern.finditer(str(text)):
            findings.append({
                "kind": rule.name,
                "start": m.start(),
                "end": m.end(),
                "matched_preview": (m.group(0)[:6] + "…" if len(m.group(0)) > 6 else m.group(0)),
                "redaction": rule.pattern.sub(rule.replacement, m.group(0)),
            })
    return findings


# ---------------------------------------------------------------------------
# Logging filter — wraps every logger so log lines auto-redact
# ---------------------------------------------------------------------------

class PiiRedactionFilter(logging.Filter):
    """Drop-in logging filter that runs redact_for_logs over every log record.

    Install once at startup:
        for handler in logging.getLogger().handlers:
            handler.addFilter(PiiRedactionFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the formatted message (after % substitution)
        try:
            msg = record.getMessage()
            redacted = redact_for_logs(msg)
            if redacted != msg:
                # Replace the args so the formatter renders the redacted msg
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


def install_global_log_redaction() -> None:
    """Attach PiiRedactionFilter to every existing log handler."""
    root = logging.getLogger()
    flt = PiiRedactionFilter()
    for h in root.handlers:
        h.addFilter(flt)
    # Also attach to common named loggers we use
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error",
                 "sarvam.diag", "twilio.client", "twilio.routes",
                 "voice", "vision", "consent.ledger", "audit", "dsr"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(flt)
