"""Background scheduler (APScheduler) for proactive delivery (bonus).

Jobs:
  * daily_digest  — once a day, for every opted-in user with activity that day:
                    refresh their recommendation and email it as a persuasive recap.
  * vector_reconcile — hourly drift repair between SQL and the vector store.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select

from ..config import settings
from ..database import session_scope
from ..models import Event, User
from . import behavior, mailer, product_service, recommender
from .mesh_client import MeshUnavailable, chat_json

logger = logging.getLogger(__name__)

_scheduler: Optional[Any] = None


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def run_daily_digest(base_url: str = "http://localhost:8000", dry_run: bool = False) -> dict[str, Any]:
    """Refresh + email today's recommendation for every active, engaged user."""
    since = datetime.utcnow() - timedelta(hours=24)
    results: list[dict[str, Any]] = []

    with session_scope() as db:
        active_user_ids = (
            db.execute(
                select(Event.user_id)
                .where(Event.user_id.isnot(None), Event.created_at >= since)
                .group_by(Event.user_id)
                .having(func.count(Event.id) >= 3)
            )
            .scalars()
            .all()
        )

        for user_id in active_user_ids:
            user = db.get(User, user_id)
            if user is None or not user.is_active or not user.digest_opt_in:
                continue
            try:
                result = recommender.get_recommendation(db, user, force=True)
                recommendation = result.recommendation
                if recommendation is None or not recommendation.items:
                    results.append({"user": user.email, "status": "no_recommendation"})
                    continue

                profile = behavior.build_profile(db, user.id)
                copy = _digest_copy(user, profile, recommendation)
                html = mailer.render_digest_html(
                    greeting=copy["greeting"],
                    body=copy["body"],
                    closing=copy["closing"],
                    items=recommendation.items,
                    base_url=base_url,
                )
                if dry_run:
                    results.append({"user": user.email, "status": "dry_run", "subject": copy["subject"]})
                    continue

                outcome = mailer.send_email(user.email, copy["subject"], html)
                if user.agent_state:
                    user.agent_state.last_digest_sent_at = datetime.utcnow()
                    db.add(user.agent_state)
                results.append({"user": user.email, "status": "sent" if outcome["sent"] else "queued",
                                "detail": outcome.get("reason")})
            except Exception as exc:  # noqa: BLE001 - one bad user must not kill the job
                logger.exception("Digest failed for user %s", user_id)
                results.append({"user": getattr(user, "email", user_id), "status": "error",
                                "detail": str(exc)[:200]})

    logger.info("daily_digest finished: %s users processed", len(results))
    return {"processed": len(results), "results": results}


def _digest_copy(user: User, profile: dict[str, Any], recommendation: Any) -> dict[str, str]:
    """Generate the email copy through Mesh, with a deterministic fallback."""
    from ..agent import prompts

    name = user.name or user.email.split("@")[0]
    try:
        result = chat_json(
            [
                {"role": "system", "content": prompts.DIGEST_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.digest_user_prompt(
                        name, profile.get("summary", ""), recommendation.headline,
                        recommendation.narrative, recommendation.items
                    ),
                },
            ],
            temperature=0.7,
            max_tokens=550,
        )
        return {
            "subject": str(result.get("subject") or "").strip()[:120] or f"Your next step, {name}",
            "greeting": str(result.get("greeting") or "").strip() or f"Hi {name},",
            "body": str(result.get("body") or "").strip() or recommendation.narrative,
            "closing": str(result.get("closing") or "").strip() or "See you in there.",
        }
    except (MeshUnavailable, ValueError) as exc:
        logger.warning("Digest copy fell back to template: %s", str(exc)[:140])
        cats = [c["name"] for c in profile.get("top_categories", [])[:1]]
        focus = cats[0] if cats else "your interests"
        return {
            "subject": f"Picking up where you left off in {focus}"[:120],
            "greeting": f"Hi {name},",
            "body": recommendation.narrative or f"Here is what lines up with your work in {focus}.",
            "closing": "See you in there.",
        }


def run_vector_reconcile() -> dict[str, Any]:
    with session_scope() as db:
        report = product_service.reconcile(db)
    logger.info("vector_reconcile: %s", report)
    return report


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def start() -> Optional[Any]:
    global _scheduler
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover
        logger.warning("APScheduler unavailable (%s) — proactive delivery disabled", exc)
        return None

    scheduler = BackgroundScheduler(timezone=settings.digest_timezone)
    scheduler.add_job(
        run_daily_digest,
        CronTrigger(
            hour=settings.digest_hour,
            minute=settings.digest_minute,
            timezone=settings.digest_timezone,
        ),
        id="daily_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_vector_reconcile,
        CronTrigger(minute=17, timezone=settings.digest_timezone),
        id="vector_reconcile",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started — daily digest at %02d:%02d %s",
        settings.digest_hour, settings.digest_minute, settings.digest_timezone,
    )
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def status() -> dict[str, Any]:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    return {
        "running": True,
        "timezone": settings.digest_timezone,
        "jobs": [
            {"id": j.id, "next_run": str(getattr(j, "next_run_time", None))}
            for j in _scheduler.get_jobs()
        ],
    }
