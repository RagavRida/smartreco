"""WebSocket connection manager for real-time recommendation push.

Each authenticated user can open a WebSocket at /ws/recommendations.
When the agent finishes generating a recommendation, the result is
broadcast to that user's active connections instantly — no polling needed.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks active WebSocket connections keyed by user_id."""

    def __init__(self) -> None:
        self._connections: dict[int, list[WebSocket]] = defaultdict(list)

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        """Accept a WebSocket and register it for the given user."""
        await ws.accept()
        self._connections[user_id].append(ws)
        logger.info("ws.connect user=%s connections=%s", user_id, len(self._connections[user_id]))

    def disconnect(self, user_id: int, ws: WebSocket) -> None:
        """Remove a WebSocket from the user's connection list."""
        conns = self._connections.get(user_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._connections.pop(user_id, None)
        logger.info("ws.disconnect user=%s remaining=%s", user_id, len(conns))

    async def send_to_user(self, user_id: int, data: dict[str, Any]) -> None:
        """Send a JSON message to all of a user's active WebSocket connections."""
        conns = self._connections.get(user_id, [])
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send a message to ALL connected users (e.g. system notifications)."""
        for user_id in list(self._connections.keys()):
            await self.send_to_user(user_id, data)

    def active_users(self) -> list[int]:
        """Return list of user_ids with active connections."""
        return list(self._connections.keys())

    def connection_count(self, user_id: int | None = None) -> int:
        """Return total connections or connections for a specific user."""
        if user_id is not None:
            return len(self._connections.get(user_id, []))
        return sum(len(conns) for conns in self._connections.values())


# Singleton instance
manager = ConnectionManager()
