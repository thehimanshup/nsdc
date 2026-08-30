"""Outbound Twilio Messages API client.

LIVE mode  — calls https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json
MOCK mode  — logs the would-be outbound and returns a synthetic MessageSid.

In MOCK mode every send returns immediately without touching the network,
so the orchestrator's dispatch logic can be tested without a Twilio account.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("twilio.client")


class TwilioClient:
    BASE_URL = "https://api.twilio.com/2010-04-01"

    @property
    def mock_mode(self) -> bool:
        return settings.twilio_mock_mode

    @property
    def from_whatsapp(self) -> str:
        return settings.twilio_whatsapp_from

    def _wa_address(self, msisdn: str) -> str:
        """Turn a 10-digit Indian mobile into Twilio's whatsapp: address."""
        m = msisdn.strip().lstrip("+").replace(" ", "")
        if m.startswith("91") and len(m) > 10:
            m = m[-10:]
        return f"whatsapp:+91{m[-10:]}"

    # -----------------------------------------------------------------
    # Send a freeform WhatsApp message (within the 24h customer-service
    # window — outside it, you'd need a templated message).
    # -----------------------------------------------------------------
    async def send_whatsapp_text(
        self,
        *,
        to_msisdn: str,
        body: str,
        media_url: Optional[str] = None,
        status_callback: Optional[str] = None,
    ) -> dict:
        to = self._wa_address(to_msisdn)
        if self.mock_mode:
            if not settings.allow_mock_providers:
                raise RuntimeError("Twilio is not configured and mock fallback is disabled")
            sid = f"SMmock_{uuid.uuid4().hex[:14]}"
            log.info("[TWILIO MOCK] WhatsApp ⇒ %s | body=%r | media=%s | sid=%s",
                     to, body[:120], media_url, sid)
            return {
                "sid": sid, "status": "queued", "from": self.from_whatsapp, "to": to,
                "body": body, "num_media": "1" if media_url else "0", "mock": True,
            }

        data: dict[str, str] = {
            "From": self.from_whatsapp,
            "To": to,
            "Body": body[:1600],   # WhatsApp body limit
        }
        if media_url:
            data["MediaUrl"] = media_url
        if status_callback:
            data["StatusCallback"] = status_callback

        async with httpx.AsyncClient(
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=httpx.Timeout(30.0, read=60.0),
        ) as c:
            url = f"{self.BASE_URL}/Accounts/{settings.twilio_account_sid}/Messages.json"
            r = await c.post(url, data=data)
            r.raise_for_status()
            obj = r.json()
            log.info("Twilio WhatsApp sent: sid=%s status=%s", obj.get("sid"), obj.get("status"))
            return obj

    # -----------------------------------------------------------------
    # Place an OUTBOUND voice call that bridges its audio to our Media
    # Streams WebSocket (Callback Agent Platform). Twilio dials `to`, and
    # once answered opens a WS to `stream_wss_url`; our handler runs the
    # Sarvam STT/TTS turn loop. Returns the Twilio Call resource (or a mock).
    # -----------------------------------------------------------------
    async def place_stream_call(self, *, to: str, stream_wss_url: str,
                                status_callback: Optional[str] = None) -> dict:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Connect>'
            f'<Stream url="{stream_wss_url}"/>'
            '</Connect></Response>'
        )
        if self.mock_mode:
            if not settings.allow_mock_providers:
                raise RuntimeError("Twilio is not configured and mock fallback is disabled")
            sid = f"CAmock_{uuid.uuid4().hex[:14]}"
            log.info("[TWILIO MOCK] Call ⇒ %s | stream=%s | sid=%s", to, stream_wss_url, sid)
            return {"sid": sid, "status": "queued", "to": to, "mock": True}

        if not settings.twilio_voice_from:
            raise RuntimeError("TWILIO_VOICE_FROM is required to place outbound calls")
        data: dict[str, str] = {
            "From": settings.twilio_voice_from,
            "To": to if to.startswith("+") else f"+{to}",
            "Twiml": twiml,
        }
        if status_callback:
            data["StatusCallback"] = status_callback
        async with httpx.AsyncClient(
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=httpx.Timeout(30.0, read=60.0),
        ) as c:
            url = f"{self.BASE_URL}/Accounts/{settings.twilio_account_sid}/Calls.json"
            r = await c.post(url, data=data)
            r.raise_for_status()
            obj = r.json()
            log.info("Twilio call placed: sid=%s status=%s to=%s",
                     obj.get("sid"), obj.get("status"), to)
            return obj

    # -----------------------------------------------------------------
    # Download media that arrived on an inbound webhook
    # -----------------------------------------------------------------
    async def fetch_media(self, media_url: str) -> tuple[bytes, str]:
        """Twilio Media URLs require auth. Returns (bytes, content_type)."""
        if self.mock_mode or not media_url:
            if not settings.allow_mock_providers:
                raise RuntimeError("Twilio media fetch unavailable: Twilio is not configured and mock fallback is disabled")
            return b"", "audio/wav"
        async with httpx.AsyncClient(
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=httpx.Timeout(30.0, read=60.0),
        ) as c:
            r = await c.get(media_url, follow_redirects=True)
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "audio/wav")


twilio_client = TwilioClient()
