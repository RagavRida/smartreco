"""Behavioural event ingestion.

Design notes:
  * The client batches; the server bulk-inserts. One round trip, one INSERT.
  * The response is returned before any recommendation work happens — tracking is
    never on the critical path of the user's experience.
  * Unknown/oversized payloads are clamped rather than rejected: losing a tracking
    event is always preferable to erroring in the user's browser.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Event, Product, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

ALLOWED_TYPES = {
    "page_view", "product_view", "search", "click", "dwell", "scroll_depth", "add_to_cart",
}


class EventIn(BaseModel):
    type: str = Field(default="page_view")
    product_id: Optional[int] = None
    slug: Optional[str] = None
    category: Optional[str] = ""
    query: Optional[str] = ""
    path: Optional[str] = ""
    dwell_ms: Optional[int] = 0
    value: Optional[float] = 0.0
    session_id: Optional[str] = ""
    anon_id: Optional[str] = ""
    ts: Optional[int] = None  # client epoch millis
    meta: Optional[dict[str, Any]] = None


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(default_factory=list)


def _resolve_product_ids(db: Session, batch: list[EventIn]) -> dict[str, int]:
    slugs = {e.slug for e in batch if e.slug and not e.product_id}
    if not slugs:
        return {}
    rows = db.execute(select(Product.slug, Product.id).where(Product.slug.in_(slugs))).all()
    return {slug: pid for slug, pid in rows}


def _to_model(raw: EventIn, user: Optional[User], slug_map: dict[str, int]) -> Optional[Event]:
    event_type = (raw.type or "").strip()
    if event_type not in ALLOWED_TYPES:
        return None

    product_id = raw.product_id or (slug_map.get(raw.slug) if raw.slug else None)
    client_ts = None
    if raw.ts:
        try:
            client_ts = datetime.utcfromtimestamp(raw.ts / 1000.0)
        except (ValueError, OSError, OverflowError):
            client_ts = None

    meta = raw.meta if isinstance(raw.meta, dict) else {}
    try:
        meta_json = json.dumps(meta)[:4000]
    except (TypeError, ValueError):
        meta_json = "{}"

    return Event(
        user_id=user.id if user else None,
        anon_id=(raw.anon_id or "")[:64],
        session_id=(raw.session_id or "")[:64],
        event_type=event_type,
        product_id=product_id,
        category=(raw.category or "")[:80],
        query=(raw.query or "")[:255],
        path=(raw.path or "")[:255],
        dwell_ms=max(0, min(int(raw.dwell_ms or 0), 3_600_000)),
        value=float(raw.value or 0.0),
        meta_json=meta_json,
        client_ts=client_ts,
    )


@router.post("/batch")
async def ingest_batch(
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    """Accepts a batch of events. Also handles navigator.sendBeacon payloads."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - beacons can arrive as text/plain
        body = (await request.body()).decode("utf-8", errors="ignore")
        try:
            payload = json.loads(body or "{}")
        except ValueError:
            return JSONResponse({"accepted": 0, "error": "invalid payload"}, status_code=400)

    if isinstance(payload, list):
        payload = {"events": payload}
    try:
        batch = EventBatchIn(**payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("event batch rejected: %s", str(exc)[:200])
        return JSONResponse({"accepted": 0, "error": "invalid schema"}, status_code=400)

    incoming = batch.events[: settings.events_max_batch]
    slug_map = _resolve_product_ids(db, incoming)

    rows = [m for m in (_to_model(e, user, slug_map) for e in incoming) if m is not None]
    if rows:
        db.bulk_save_objects(rows)   # one round trip, no per-object ORM overhead
        db.commit()

    return JSONResponse({"accepted": len(rows), "dropped": len(incoming) - len(rows)}, status_code=202)


@router.get("/mine")
def my_events(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    """Small introspection endpoint — handy for the demo and for debugging."""
    if user is None:
        return JSONResponse({"events": [], "total": 0})
    total = db.execute(
        select(func.count(Event.id)).where(Event.user_id == user.id)
    ).scalar_one()
    rows = (
        db.execute(
            select(Event)
            .where(Event.user_id == user.id)
            .order_by(Event.created_at.desc())
            .limit(min(limit, 200))
        )
        .scalars()
        .all()
    )
    return {
        "total": int(total),
        "events": [
            {
                "id": e.id,
                "type": e.event_type,
                "product_id": e.product_id,
                "category": e.category,
                "query": e.query,
                "path": e.path,
                "dwell_ms": e.dwell_ms,
                "created_at": e.created_at.isoformat(),
            }
            for e in rows
        ],
    }
