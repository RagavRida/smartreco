"""WebSocket endpoint for real-time recommendation updates.

Usage:
  const ws = new WebSocket(`ws://${location.host}/ws/recommendations`);
  ws.onmessage = (e) => { const data = JSON.parse(e.data); ... };
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import SESSION_COOKIE, read_session
from ..services.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


def _authenticate_ws(ws: WebSocket, db: Session) -> User | None:
    """Authenticate a WebSocket via session cookie (browser) or query param token."""
    # Try session cookie first (browser connections)
    token = ws.cookies.get(SESSION_COOKIE)
    if token:
        user_id = read_session(token)
        if user_id is not None:
            user = db.get(User, user_id)
            if user and user.is_active:
                return user

    # Try query param ?token=... (programmatic connections)
    token_param = ws.query_params.get("token")
    if token_param:
        from ..security import decode_token
        payload = decode_token(token_param)
        if payload and payload.get("type") == "access":
            try:
                user_id = int(payload["sub"])
            except (KeyError, ValueError, TypeError):
                return None
            user = db.get(User, user_id)
            if user and user.is_active:
                return user

    return None


@router.websocket("/ws/recommendations")
async def ws_recommendations(ws: WebSocket, db: Session = Depends(get_db)):
    """WebSocket endpoint that pushes real-time recommendation updates.

    On connect: sends a welcome message with connection status.
    On agent run: recommendation data is pushed automatically.
    Client can send 'ping' to keep alive; server responds with 'pong'.
    """
    user = _authenticate_ws(ws, db)
    if user is None:
        await ws.close(code=4001, reason="Authentication required")
        return

    await manager.connect(user.id, ws)

    # Send welcome message
    await ws.send_json({
        "type": "connected",
        "user_id": user.id,
        "message": "Real-time recommendation feed active",
    })

    try:
        while True:
            # Wait for client messages (keepalive pings)
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=300)
                if data == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send a server-side ping to check if connection is alive
                try:
                    await ws.send_json({"type": "heartbeat"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("ws error user=%s: %s", user.id, exc)
    finally:
        manager.disconnect(user.id, ws)
