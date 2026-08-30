"""Conversation store. In-memory + JSON file persistence.

Phase 1 keeps this dead simple. Phase 2 swaps in SQLite; Phase 7 swaps to
PostgreSQL. The interface stays the same — only the implementation changes.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

from .config import settings
from .models import Message


_LOCK = threading.RLock()


class ConversationStore:
    """One conversation per (citizenId, agentId) pair. Identified by convId."""

    def __init__(self) -> None:
        # citizenId -> { msisdn, language }
        self.citizens: dict[str, dict] = {}
        # msisdn -> citizenId
        self.msisdn_to_id: dict[str, str] = {}
        # convId -> list[Message]
        self.conversations: dict[str, list[Message]] = {}
        # convId -> { citizenId, agentId, lastMsgAt }
        self.conv_meta: dict[str, dict] = {}
        self._load()

    # -----------------------------------------------------------------
    # Citizens
    # -----------------------------------------------------------------
    def get_or_create_citizen(self, msisdn: str) -> str:
        msisdn = msisdn.strip().lstrip("+").lstrip("91")[-10:]
        with _LOCK:
            cid = self.msisdn_to_id.get(msisdn)
            if cid:
                return cid
            cid = f"ctz_{uuid.uuid4().hex[:12]}"
            self.citizens[cid] = {
                "msisdn": msisdn,
                "language": "en-IN",
                "createdAt": datetime.utcnow().isoformat(),
            }
            self.msisdn_to_id[msisdn] = cid
            self._persist()
            return cid

    def set_citizen_language(self, citizen_id: str, lang: str) -> None:
        with _LOCK:
            if citizen_id in self.citizens:
                self.citizens[citizen_id]["language"] = lang
                self._persist()

    def set_citizen_state(self, citizen_id: str, state_code: str,
                           primary_language: str = "") -> None:
        """Phase 6d — record the citizen's state. Updates language only if
        the citizen hasn't explicitly typed in another language yet."""
        with _LOCK:
            c = self.citizens.get(citizen_id)
            if not c:
                return
            c["state_code"] = state_code.upper()
            # Set language to state default only on FIRST state assignment,
            # so the orchestrator's per-message language detection can still
            # override it later if the citizen actually types in another script.
            if not c.get("language_explicit") and primary_language:
                c["language"] = primary_language
            self._persist()

    def get_citizen(self, citizen_id: str) -> Optional[dict]:
        return self.citizens.get(citizen_id)

    # -----------------------------------------------------------------
    # Conversations
    # -----------------------------------------------------------------
    def conv_id(self, citizen_id: str, agent_id: str) -> str:
        return f"{citizen_id}:{agent_id}"

    def get_or_create_conv(self, citizen_id: str, agent_id: str) -> str:
        cid = self.conv_id(citizen_id, agent_id)
        with _LOCK:
            if cid not in self.conversations:
                self.conversations[cid] = []
                self.conv_meta[cid] = {
                    "citizenId": citizen_id,
                    "agentId": agent_id,
                    "createdAt": datetime.utcnow().isoformat(),
                    "lastMsgAt": None,
                }
        return cid

    def append(self, msg: Message) -> Message:
        with _LOCK:
            convo = self.conversations.setdefault(msg.convId, [])
            convo.append(msg)
            meta = self.conv_meta.setdefault(msg.convId, {})
            meta["lastMsgAt"] = msg.timestamp.isoformat()
            self._persist()
            return msg

    def history(self, conv_id: str, limit: int = 20) -> list[Message]:
        return list(self.conversations.get(conv_id, []))[-limit:]

    def as_chat_messages(self, conv_id: str, limit: int = 10) -> list[dict]:
        """Format last N messages as Sarvam chat-completion 'messages' array.

        Skips messages with empty content — Sarvam returns HTTP 400 on
        `{"role": "user", "content": ""}` payloads.
        """
        msgs = self.history(conv_id, limit=limit)
        out: list[dict] = []
        for m in msgs:
            text = (m.text or "").strip()
            if not text:
                continue
            if m.role == "user":
                out.append({"role": "user", "content": text})
            elif m.role == "agent":
                out.append({"role": "assistant", "content": text})
            elif m.role == "system" and m.type == "tool_result":
                tool_id = (m.extra or {}).get("toolId")
                if tool_id == "vision.extract_document":
                    result = (m.extra or {}).get("result") or {}
                    doc = result.get("document") or {}
                    summary = {
                        "document_type": result.get("document_type"),
                        "confidence": result.get("confidence"),
                        "language": result.get("language"),
                        "document": doc,
                        "raw_text": result.get("raw_text") or "",
                    }
                    out.append({
                        "role": "system",
                        "content": "OCR result from uploaded document: "
                                   + json.dumps(summary, ensure_ascii=False),
                    })
        return out

    def last_previews(self, citizen_id: str) -> dict[str, dict]:
        """Map agentId -> {text, time} for chat-list preview rendering."""
        out: dict[str, dict] = {}
        with _LOCK:
            for cid, meta in self.conv_meta.items():
                if meta.get("citizenId") != citizen_id:
                    continue
                history = self.conversations.get(cid, [])
                if not history:
                    continue
                last = history[-1]
                out[meta["agentId"]] = {
                    "text": last.text[:80],
                    "time": last.timestamp.isoformat(),
                    "role": last.role,
                }
        return out

    # -----------------------------------------------------------------
    # Persistence — single JSON file, atomic write
    # -----------------------------------------------------------------
    def _path(self) -> str:
        os.makedirs(settings.data_dir, exist_ok=True)
        return os.path.join(settings.data_dir, "store.json")

    def _persist(self) -> None:
        path = self._path()
        try:
            payload = {
                "citizens": self.citizens,
                "msisdn_to_id": self.msisdn_to_id,
                "conv_meta": self.conv_meta,
                "conversations": {
                    cid: [m.model_dump(mode="json") for m in msgs]
                    for cid, msgs in self.conversations.items()
                },
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, path)
        except Exception:
            pass  # Phase 1: don't crash on persistence errors

    def _load(self) -> None:
        path = self._path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            self.citizens = payload.get("citizens", {})
            self.msisdn_to_id = payload.get("msisdn_to_id", {})
            self.conv_meta = payload.get("conv_meta", {})
            self.conversations = {
                cid: [Message(**m) for m in msgs]
                for cid, msgs in payload.get("conversations", {}).items()
            }
        except Exception:
            pass


store = ConversationStore()
