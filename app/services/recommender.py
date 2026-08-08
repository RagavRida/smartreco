"""Orchestrates the recommendation lifecycle.

    events -> behaviour profile -> trigger policy -> (agent run | cached) -> stored reco

The trigger policy is the reason this is affordable: the agent only runs when the
user's behaviour has actually changed in a way that would change the answer.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.graph import engine_name, run_graph
from ..agent.state import new_state
from ..config import settings
from ..models import Product, Recommendation, User
from . import behavior, tracing, trigger

logger = logging.getLogger(__name__)


def _broadcast_recommendation(user_id: int, recommendation: "Recommendation") -> None:
    """Fire-and-forget push of a new recommendation to the user's WebSocket connections."""
    import asyncio
    try:
        from .ws_manager import manager
        payload = {
            "type": "recommendation_updated",
            "recommendation": {
                "id": recommendation.id,
                "headline": recommendation.headline,
                "narrative": recommendation.narrative,
                "items": recommendation.items,
                "trigger_reason": recommendation.trigger_reason,
                "model_used": recommendation.model_used,
                "latency_ms": recommendation.latency_ms,
                "is_fallback": recommendation.is_fallback,
                "created_at": recommendation.created_at.isoformat() if recommendation.created_at else None,
            },
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.send_to_user(user_id, payload))
        except RuntimeError:
            # No running event loop (e.g. during sync tests) — skip silently
            pass
    except Exception:
        # WebSocket broadcast is best-effort; never block the main flow
        pass


@dataclass
class RecommendationResult:
    recommendation: Optional[Recommendation]
    ran_agent: bool
    decision: dict[str, Any]
    trace: list[dict[str, Any]]
    ab_variant: Optional[str] = None
    ab_variant_name: Optional[str] = None
    ab_experiment_id: Optional[int] = None

    @property
    def served_from_cache(self) -> bool:
        return not self.ran_agent and self.recommendation is not None


def _catalog_categories(db: Session) -> list[str]:
    rows = db.execute(
        select(Product.category).where(Product.is_published.is_(True)).distinct()
    ).scalars().all()
    return sorted({r for r in rows if r})


def get_recommendation(
    db: Session,
    user: User,
    *,
    force: bool = False,
    limit: Optional[int] = None,
) -> RecommendationResult:
    """Main entry point used by the dashboard, the API and the scheduler."""
    limit = limit or settings.rec_products_returned

    events = behavior.recent_events(db, user.id)
    profile = behavior.build_profile(db, user.id, events)
    signature = profile["signature"]

    decision = trigger.evaluate(db, user.id, signature, force=force)
    cached = trigger.active_recommendation(db, user.id)

    if not decision.should_run:
        trigger.mark_skipped(db, user.id)
        logger.info(
            "reco.skip user=%s reason=%s new_events=%s", user.id, decision.reason, decision.new_events
        )
        return RecommendationResult(cached, False, decision.as_dict(), [])

    started = time.time()
    state = new_state(
        user_id=user.id,
        user_name=user.name or user.email.split("@")[0],
        profile=profile,
        behavior_summary=profile["summary"],
        catalog_categories=_catalog_categories(db),
        limit=limit,
    )

    with tracing.trace_run(
        "smartreco.recommend",
        user_id=user.id,
        trigger=decision.reason,
        engine=engine_name(),
        events=profile["event_count"],
    ):
        final = run_graph(db, state)

    latency_ms = int((time.time() - started) * 1000)
    recommendation = _persist(db, user, final, profile, decision.reason, latency_ms)
    trigger.mark_run(db, user.id, signature)

    logger.info(
        "reco.run user=%s engine=%s items=%s refine_loops=%s latency_ms=%s fallback=%s",
        user.id,
        engine_name(),
        len(final.get("items", [])),
        final.get("refine_loops", 0),
        latency_ms,
        final.get("is_fallback"),
    )

    # Track A/B experiment impression
    try:
        from . import ab_testing
        ab_exp_id = final.get("ab_experiment_id")
        ab_variant = final.get("ab_variant")
        if ab_exp_id and ab_variant:
            ab_testing.track_impression(
                db, ab_exp_id, user.id, ab_variant, recommendation.id
            )
    except Exception:
        pass  # best-effort

    # Push real-time update via WebSocket
    _broadcast_recommendation(user.id, recommendation)

    result = RecommendationResult(recommendation, True, decision.as_dict(), final.get("trace", []))
    result.ab_variant = final.get("ab_variant")
    result.ab_variant_name = final.get("ab_variant_name")
    result.ab_experiment_id = final.get("ab_experiment_id")
    return result


def _persist(
    db: Session,
    user: User,
    final: dict[str, Any],
    profile: dict[str, Any],
    trigger_reason: str,
    latency_ms: int,
) -> Recommendation:
    """Store the new recommendation and retire the previous one (history is kept)."""
    previous = (
        db.execute(
            select(Recommendation).where(
                Recommendation.user_id == user.id, Recommendation.is_active.is_(True)
            )
        )
        .scalars()
        .all()
    )
    for row in previous:
        row.is_active = False
        db.add(row)

    profile_for_storage = {k: v for k, v in profile.items() if k not in {"window_started", "window_ended"}}

    recommendation = Recommendation(
        user_id=user.id,
        headline=final.get("headline", "")[:255],
        narrative=final.get("narrative", ""),
        items_json=json.dumps(final.get("items", []), default=str),
        interest_profile_json=json.dumps(profile_for_storage, default=str),
        interest_signature=profile.get("signature", ""),
        trigger_reason=trigger_reason[:120],
        model_used=final.get("model_used", "")[:120],
        retrieval_queries=json.dumps(final.get("queries", [])),
        refine_loops=int(final.get("refine_loops", 0)),
        events_considered=int(profile.get("event_count", 0)),
        latency_ms=latency_ms,
        is_fallback=bool(final.get("is_fallback")),
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)
    return recommendation


def history(db: Session, user_id: int, limit: int = 10) -> list[Recommendation]:
    return list(
        db.execute(
            select(Recommendation)
            .where(Recommendation.user_id == user_id)
            .order_by(Recommendation.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
