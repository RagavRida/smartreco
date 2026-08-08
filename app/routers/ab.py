"""A/B testing API — experiment results, click tracking, and admin controls."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_api_user, require_admin
from ..models import User
from ..services import ab_testing

router = APIRouter(prefix="/api/ab", tags=["ab-testing"])


# ---------------------------------------------------------------------------
# Click tracking (called from the dashboard when a user clicks a reco item)
# ---------------------------------------------------------------------------

class ClickEvent(BaseModel):
    product_id: int
    experiment_id: int
    variant: str
    recommendation_id: int | None = None


@router.post("/click")
async def track_click(request: Request, db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    """Record a click on a recommended course for A/B tracking.
    Accepts both JSON body and sendBeacon text/plain payloads."""
    import json as jsonlib
    try:
        body = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8", errors="ignore")
        try:
            body = jsonlib.loads(raw)
        except (ValueError, TypeError):
            return {"status": "skipped", "reason": "invalid payload"}

    product_id = body.get("product_id")
    experiment_id = body.get("experiment_id")
    variant = body.get("variant")

    if not all([product_id, experiment_id, variant]):
        return {"status": "skipped", "reason": "missing fields"}

    ab_testing.track_click(
        db,
        experiment_id=int(experiment_id),
        user_id=user.id,
        variant=str(variant),
        product_id=int(product_id),
        recommendation_id=int(body["recommendation_id"]) if body.get("recommendation_id") else None,
    )
    return {"status": "tracked"}


# ---------------------------------------------------------------------------
# Experiment results (admin-facing analytics)
# ---------------------------------------------------------------------------

@router.get("/results")
def experiment_results(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    """Get results for the active A/B experiment."""
    experiment = ab_testing.get_active_experiment(db)
    if experiment is None:
        return {"active": False, "message": "No active A/B experiment."}

    results = ab_testing.get_experiment_results(db, experiment.id)
    if results is None:
        return {"active": False, "message": "Experiment not found."}

    return {
        "active": True,
        "experiment": results.experiment,
        "variant_a": asdict(results.variant_a),
        "variant_b": asdict(results.variant_b),
        "winner": results.winner,
        "winner_name": results.winner_name,
        "lift_pct": results.lift_pct,
        "confidence": results.confidence,
        "is_significant": results.is_significant,
        "total_events": results.total_events,
        "recommendation": results.recommendation,
    }


# ---------------------------------------------------------------------------
# User's variant assignment (so the dashboard knows which variant they're in)
# ---------------------------------------------------------------------------

@router.get("/my-variant")
def my_variant(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    """Return the current user's A/B variant assignment."""
    experiment = ab_testing.get_active_experiment(db)
    if experiment is None:
        return {"enrolled": False}

    variant = ab_testing.assign_variant(user.id, experiment.id)
    return {
        "enrolled": True,
        "experiment_id": experiment.id,
        "experiment_name": experiment.name,
        "variant": variant,
        "variant_name": ab_testing.get_variant_name(experiment, variant),
    }
