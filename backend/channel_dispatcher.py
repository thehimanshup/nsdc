"""Channel Dispatcher — routes agent replies back on the channel the
citizen used.

The orchestrator emits "frames" (typing, agent_token, agent_message, etc.).
For each frame the dispatcher decides which channels to deliver it on:

  - simulator/app: every frame (typing, tokens, tool cards, final msg)
  - twilio_wa:     only the final agent_message + tool_result + broadcast
                   (Twilio WhatsApp doesn't do token streaming)
  - twilio_voice:  (Phase 4) bridges into LiveKit, not handled here yet

This keeps the orchestrator channel-agnostic. Adding a new channel is one
elif branch.
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import settings
from .models import Message
from .store import store
from .twilio_client import twilio_client
from .ws_manager import ws_manager

log = logging.getLogger("dispatch")


# Frames that go to non-streaming channels (only the final, deliverable thing)
NON_STREAMING_DELIVERABLE = {"agent_message", "broadcast"}


class ChannelDispatcher:

    async def dispatch(
        self,
        *,
        citizen_id: str,
        frame: dict,
        primary_channel: str = "simulator",
    ) -> None:
        """Send a frame to one or more channels."""
        # Always send to WS — the simulator (or any app) may be listening.
        # WS frames are cheap; this also gives multi-device behavior for free.
        await ws_manager.send_to_citizen(citizen_id, frame)

        # If the primary channel is something else, deliver the final-form
        # version of the frame there too.
        if primary_channel == "twilio_wa" and frame.get("type") in NON_STREAMING_DELIVERABLE:
            await self._dispatch_twilio_wa(citizen_id, frame)

    async def _dispatch_twilio_wa(self, citizen_id: str, frame: dict) -> None:
        citizen = store.get_citizen(citizen_id)
        if not citizen:
            return
        msisdn = citizen.get("msisdn")
        if not msisdn:
            return

        frame_type = frame.get("type")
        body: Optional[str] = None
        media_url: Optional[str] = None

        if frame_type == "agent_message":
            m = frame.get("message", {})
            if m.get("type") in ("text", "voice"):
                body = m.get("text") or "(empty reply)"
                # If we have a Bulbul-generated audio URL AND a public base URL,
                # surface the voice reply as a Twilio media message too.
                if m.get("audioUrl") and settings.public_base_url:
                    media_url = settings.public_base_url.rstrip("/") + m["audioUrl"]
            elif m.get("type") == "tool_result":
                # Render the tool card as plain WhatsApp text
                body = self._render_tool_for_whatsapp(m)
            elif m.get("type") == "system_event":
                body = m.get("text", "")
        elif frame_type == "broadcast":
            body = f"📢 {frame.get('title', '')}\n\n{frame.get('body', '')}"

        if not body:
            return

        try:
            status_cb = (settings.public_base_url.rstrip("/") + "/webhooks/twilio/status"
                         if settings.public_base_url else None)
            result = await twilio_client.send_whatsapp_text(
                to_msisdn=msisdn, body=body, media_url=media_url,
                status_callback=status_cb,
            )
            log.info("Dispatched to Twilio WA: sid=%s mock=%s",
                     result.get("sid"), result.get("mock", False))
        except Exception as e:
            log.error("Twilio outbound failed: %s", e)

    def _render_tool_for_whatsapp(self, msg: dict) -> str:
        """Tool result cards become plain WhatsApp text (no rich UI)."""
        extra = msg.get("extra") or {}
        tool_id = extra.get("toolId", "tool")
        result = extra.get("result") or {}
        doc = result.get("document")
        if doc:
            lines = [f"📄 {tool_id}"]
            for k, v in doc.items():
                if k.startswith("is_") or k == "fetched_at":
                    continue
                lines.append(f"  • {k}: {v}")
            if result.get("is_mock"):
                lines.append("  (mock data — Phase 2 fixture)")
            return "\n".join(lines)
        if result.get("complaint_id"):
            return (f"✅ Complaint registered: {result['complaint_id']}\n"
                    f"Category: {result.get('category', '')}\n"
                    f"Expected resolution: {result.get('expected_resolution', '')}")
        if result.get("grievance_id"):
            return (f"✅ Grievance filed: {result['grievance_id']}\n"
                    f"Status: {result.get('status', '')}\n"
                    f"Track at: {result.get('track_at', '')}")
        return msg.get("text", "")


dispatcher = ChannelDispatcher()
