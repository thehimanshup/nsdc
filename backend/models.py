"""Pydantic models — Phase 3 adds a `channel` field on Messages so the
orchestrator knows where to deliver replies (simulator vs Twilio WhatsApp)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# Channel field — extensible. New channels (twilio_voice, livekit_app, etc.)
# can be added in later phases without breaking the existing ones.
Channel = Literal["simulator", "twilio_wa", "twilio_voice", "livekit_app", "system"]


class AuthInitRequest(BaseModel):
    msisdn: str = Field(..., description="10-digit mobile number")
    state_code: Optional[str] = Field(None, description="Citizen's state — auto-detected from msisdn if not provided")


class AuthInitResponse(BaseModel):
    citizenId: str
    msisdn: str
    wsToken: str
    # Phase 6d — state + language hints sent back to the simulator
    stateCode: Optional[str] = None
    stateName: Optional[str] = None
    stateEmoji: Optional[str] = None
    primaryLanguage: Optional[str] = None
    stateAutoDetected: bool = False


class AgentMeta(BaseModel):
    id: str
    name: str
    emoji: str
    color: str
    bg: str
    description: str
    pinned: bool = False
    languages: list[str] = Field(default_factory=list)
    voice: str = "shubh"
    tools: list[str] = Field(default_factory=list)


class AgentListResponse(BaseModel):
    agents: list[AgentMeta]


MessageRole = Literal["user", "agent", "system"]
MessageType = Literal["text", "voice", "media", "system_event", "tool_call", "tool_result"]


class Message(BaseModel):
    id: str
    convId: str
    role: MessageRole
    type: MessageType
    text: str = ""
    lang: str = "en-IN"
    mediaUrl: Optional[str] = None
    audioUrl: Optional[str] = None
    durationSec: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    clientMsgId: Optional[str] = None
    extra: dict = Field(default_factory=dict)
    # Phase 3 additions
    channel: Channel = "simulator"
    providerMessageId: Optional[str] = None     # e.g., Twilio MessageSid
    providerStatus: Optional[str] = None        # queued|sent|delivered|read|failed


class SendMessageRequest(BaseModel):
    text: str
    lang: Optional[str] = None
    clientMsgId: Optional[str] = None


class SendMessageResponse(BaseModel):
    accepted: bool
    serverMsgId: str
    convId: str


class ConversationHistoryResponse(BaseModel):
    convId: str
    agentId: str
    messages: list[Message]


class ConsentDecisionRequest(BaseModel):
    decision: Literal["granted", "denied"]


class TemplateRender(BaseModel):
    """Used by the broadcast/templated-message API."""
    name: str
    language: str = "en-IN"
    variables: dict[str, str] = Field(default_factory=dict)


class TestTwilioInboundRequest(BaseModel):
    """Simulate a Twilio inbound WhatsApp message — for local testing
    without a real Twilio account."""
    from_msisdn: str = Field(..., description="Sender's 10-digit mobile")
    body: str = Field("", description="Message text")
    agent_id: Optional[str] = Field(None, description="Optional agent override")


class ImageUploadHint(BaseModel):
    """Phase 4: explicit hint to Sarvam Vision about the document type."""
    hint_type: Optional[str] = Field(
        None, description="One of: pan, aadhaar, driving_licence, voter_id, "
                          "ration_card_image, patta_image, auto"
    )
