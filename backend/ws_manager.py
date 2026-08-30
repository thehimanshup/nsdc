"""WebSocket connection manager — keyed by citizenId.

One citizen can have multiple connected clients (web + simulator). We
broadcast each push frame to all of them.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        # citizenId -> set of WebSockets
        self._conns: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, citizen_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.setdefault(citizen_id, set()).add(ws)

    async def disconnect(self, citizen_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if citizen_id in self._conns:
                self._conns[citizen_id].discard(ws)
                if not self._conns[citizen_id]:
                    del self._conns[citizen_id]

    async def send_to_citizen(self, citizen_id: str, frame: dict[str, Any]) -> None:
        async with self._lock:
            conns = list(self._conns.get(citizen_id, []))
        if not conns:
            return
        msg = json.dumps(frame, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    if citizen_id in self._conns:
                        self._conns[citizen_id].discard(ws)

    async def broadcast(self, frame: dict[str, Any]) -> None:
        """Send frame to every connected citizen (used by broadcast demo)."""
        async with self._lock:
            ids = list(self._conns.keys())
        await asyncio.gather(*(self.send_to_citizen(cid, frame) for cid in ids))

    @property
    def connected_count(self) -> int:
        return sum(len(s) for s in self._conns.values())


ws_manager = WSManager()
