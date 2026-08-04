"""Trigger policy — decides whether running the agent is worth an LLM call.

Rules (first match wins):
  1. No recommendation on file yet, and the user has any activity -> RUN (cold start)
  2. Explicit force (admin / scheduler / user pressed refresh)         -> RUN
  3. Ran within REC_MIN_SECONDS_BETWEEN_RUNS                           -> SKIP (rate limit)
  4. Interest signature changed                                        -> RUN (behaviour shifted)
  5. >= REC_MIN_NEW_EVENTS significant new events                      -> RUN (enough new signal)
  6. Cached recommendation older than TTL and there is new activity    -> RUN (staleness)
  7. Otherwise                                                         -> SKIP (serve cache)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentState, Event, Recommendation
from .behavior import SIGNIFICANT_EVENTS


@dataclass
class TriggerDecision:
    should_run: bool
    reason: str
    new_events: int
    signature_changed: bool
    cached_age_seconds: Optional[float]

    def as_dict(self) -> dict:
        return {
            "should_run": self.should_run,
            "reason": self.reason,
            "new_events": self.new_events,
            "signature_changed": self.signature_changed,
            "cached_age_seconds": self.cached_age_seconds,
        }


def get_or_create_state(db: Session, user_id: int) -> AgentState:
    state = db.execute(select(AgentState).where(AgentState.user_id == user_id)).scalar_one_or_none()
    if state is None:
        state = AgentState(user_id=user_id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def active_recommendation(db: Session, user_id: int) -> Optional[Recommendation]:
    return db.execute(
        select(Recommendation)
        .where(Recommendation.user_id == user_id, Recommendation.is_active.is_(True))
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def count_new_significant_events(db: Session, user_id: int, since_event_id: int) -> int:
    return int(
        db.execute(
            select(func.count(Event.id)).where(
                Event.user_id == user_id,
                Event.id > since_event_id,
                Event.event_type.in_(tuple(SIGNIFICANT_EVENTS)),
            )
        ).scalar_one()
    )


def total_events(db: Session, user_id: int) -> int:
    return int(
        db.execute(select(func.count(Event.id)).where(Event.user_id == user_id)).scalar_one()
    )


def evaluate(
    db: Session,
    user_id: int,
    current_signature: str,
    *,
    force: bool = False,
) -> TriggerDecision:
    state = get_or_create_state(db, user_id)
    cached = active_recommendation(db, user_id)
    new_events = count_new_significant_events(db, user_id, state.last_event_id_seen or 0)
    signature_changed = bool(current_signature) and current_signature != (state.last_signature or "")
    now = datetime.utcnow()
    cached_age = (now - cached.created_at).total_seconds() if cached else None

    if force:
        return TriggerDecision(True, "forced refresh", new_events, signature_changed, cached_age)

    if cached is None:
        if total_events(db, user_id) == 0:
            return TriggerDecision(False, "no activity to reason about", 0, False, None)
        return TriggerDecision(True, "cold start — first recommendation", new_events, signature_changed, None)

    if state.last_run_at and now - state.last_run_at < timedelta(
        seconds=settings.rec_min_seconds_between_runs
    ):
        return TriggerDecision(
            False, "rate limited — agent ran moments ago", new_events, signature_changed, cached_age
        )

    if signature_changed and new_events > 0:
        return TriggerDecision(
            True, "interest profile shifted", new_events, True, cached_age
        )

    if new_events >= settings.rec_min_new_events:
        return TriggerDecision(
            True, f"{new_events} new significant events", new_events, signature_changed, cached_age
        )

    if cached_age is not None and cached_age > settings.rec_cache_ttl_seconds and new_events > 0:
        return TriggerDecision(
            True, "cached recommendation went stale", new_events, signature_changed, cached_age
        )

    return TriggerDecision(
        False, "cache still valid — no meaningful change", new_events, signature_changed, cached_age
    )


def mark_run(db: Session, user_id: int, signature: str) -> None:
    state = get_or_create_state(db, user_id)
    latest_event_id = db.execute(
        select(func.coalesce(func.max(Event.id), 0)).where(Event.user_id == user_id)
    ).scalar_one()
    state.last_run_at = datetime.utcnow()
    state.last_event_id_seen = int(latest_event_id or 0)
    state.last_signature = signature
    state.runs_total = (state.runs_total or 0) + 1
    db.add(state)
    db.commit()


def mark_skipped(db: Session, user_id: int) -> None:
    state = get_or_create_state(db, user_id)
    state.runs_skipped = (state.runs_skipped or 0) + 1
    db.add(state)
    db.commit()
