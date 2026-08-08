"""Recommendation API — what the dashboard polls and what the demo pokes at."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_api_user
from ..models import User
from ..services import behavior, recommender, trigger
from ..agent.graph import engine_name

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


def _serialize(result: recommender.RecommendationResult) -> dict:
    reco = result.recommendation
    return {
        "ran_agent": result.ran_agent,
        "served_from_cache": result.served_from_cache,
        "decision": result.decision,
        "engine": engine_name(),
        "recommendation": None
        if reco is None
        else {
            "id": reco.id,
            "headline": reco.headline,
            "narrative": reco.narrative,
            "items": reco.items,
            "trigger_reason": reco.trigger_reason,
            "model_used": reco.model_used,
            "refine_loops": reco.refine_loops,
            "events_considered": reco.events_considered,
            "latency_ms": reco.latency_ms,
            "is_fallback": reco.is_fallback,
            "created_at": reco.created_at.isoformat(),
        },
        "trace": result.trace,
    }


@router.get("")
def current(
    refresh: bool = Query(False, description="Force the agent to run"),
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    result = recommender.get_recommendation(db, user, force=refresh)
    return _serialize(result)


@router.post("/refresh")
def refresh(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    result = recommender.get_recommendation(db, user, force=True)
    return _serialize(result)


@router.get("/profile")
def profile(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    """The behavioural profile the agent reasons over — useful for the demo."""
    prof = behavior.build_profile(db, user.id)
    state = trigger.get_or_create_state(db, user.id)
    decision = trigger.evaluate(db, user.id, prof["signature"])
    return {
        "profile": prof,
        "trigger": decision.as_dict(),
        "agent_state": {
            "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
            "runs_total": state.runs_total,
            "runs_skipped": state.runs_skipped,
            "last_signature": state.last_signature,
        },
    }


@router.get("/history")
def history(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    rows = recommender.history(db, user.id)
    return {
        "history": [
            {
                "id": r.id,
                "headline": r.headline,
                "narrative": r.narrative,
                "items": [i.get("title") for i in r.items],
                "trigger_reason": r.trigger_reason,
                "created_at": r.created_at.isoformat(),
                "is_active": r.is_active,
            }
            for r in rows
        ]
    }
