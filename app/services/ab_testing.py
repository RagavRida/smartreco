"""A/B testing service for self-improving recommendations.

Manages experiments that compare two recommendation styles (e.g. persuasive vs
informational). Users are deterministically assigned to a variant based on their
user_id, and every impression/click is tracked for statistical comparison.

The system learns which style performs better and auto-promotes the winner.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ABEvent, ABExperiment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Two built-in recommendation style prompts
# ---------------------------------------------------------------------------

STYLE_PERSUASIVE = """You write the personalised recommendation block on a learner's \
dashboard at an online course marketplace.

Your style is PERSUASIVE: warm, motivating, and action-oriented. You make the learner \
feel like now is the moment. Reference their actual behaviour to build credibility, \
then create urgency through opportunity — not scarcity. Use vivid, energetic language \
that makes the next step feel exciting and inevitable.

Speak to them as "you". Confident and encouraging. Not salesy, not corporate."""

STYLE_INFORMATIONAL = """You write the personalised recommendation block on a learner's \
dashboard at an online course marketplace.

Your style is INFORMATIONAL: clear, analytical, and evidence-based. You present courses \
as logical next steps based on the data. Reference their actual behaviour to show you \
understand their learning path, then explain WHY each course fits — citing skill gaps, \
topic adjacency, or progression logic.

Speak to them as "you". Thoughtful and precise. Like a knowledgeable advisor, not a salesperson."""

DEFAULT_EXPERIMENTS = [
    {
        "name": "Persuasive vs Informational",
        "description": "Tests whether motivating, action-oriented copy drives more clicks than calm, analytical copy.",
        "variant_a_name": "persuasive",
        "variant_b_name": "informational",
        "variant_a_prompt": STYLE_PERSUASIVE,
        "variant_b_prompt": STYLE_INFORMATIONAL,
    }
]


# ---------------------------------------------------------------------------
# Variant assignment (deterministic, no randomness)
# ---------------------------------------------------------------------------

def assign_variant(user_id: int, experiment_id: int) -> str:
    """Deterministically assign a user to variant A or B.

    Uses a hash so the same user always gets the same variant for a given
    experiment, but different experiments can produce different assignments.
    """
    seed = f"{experiment_id}:{user_id}"
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return "A" if int(digest[:8], 16) % 2 == 0 else "B"


# ---------------------------------------------------------------------------
# Experiment management
# ---------------------------------------------------------------------------

def get_active_experiment(db: Session) -> Optional[ABExperiment]:
    """Return the currently active A/B experiment, or None."""
    return db.execute(
        select(ABExperiment).where(ABExperiment.is_active.is_(True)).limit(1)
    ).scalar_one_or_none()


def ensure_default_experiment(db: Session) -> ABExperiment:
    """Create the default experiment if none exists."""
    existing = get_active_experiment(db)
    if existing:
        return existing

    cfg = DEFAULT_EXPERIMENTS[0]
    experiment = ABExperiment(**cfg)
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    logger.info("ab_test.created experiment=%s name=%s", experiment.id, experiment.name)
    return experiment


def get_variant_prompt(experiment: ABExperiment, variant: str) -> str:
    """Return the system prompt override for a given variant."""
    if variant == "A":
        return experiment.variant_a_prompt
    return experiment.variant_b_prompt


def get_variant_name(experiment: ABExperiment, variant: str) -> str:
    """Return the human-readable name for a variant."""
    if variant == "A":
        return experiment.variant_a_name
    return experiment.variant_b_name


# ---------------------------------------------------------------------------
# Event tracking
# ---------------------------------------------------------------------------

def track_impression(
    db: Session,
    experiment_id: int,
    user_id: int,
    variant: str,
    recommendation_id: Optional[int] = None,
) -> ABEvent:
    """Record that a user saw a recommendation in their assigned variant."""
    event = ABEvent(
        experiment_id=experiment_id,
        user_id=user_id,
        variant=variant,
        event_type="impression",
        recommendation_id=recommendation_id,
    )
    db.add(event)
    db.commit()
    return event


def track_click(
    db: Session,
    experiment_id: int,
    user_id: int,
    variant: str,
    product_id: int,
    recommendation_id: Optional[int] = None,
) -> ABEvent:
    """Record that a user clicked a recommended course."""
    event = ABEvent(
        experiment_id=experiment_id,
        user_id=user_id,
        variant=variant,
        event_type="click",
        product_id=product_id,
        recommendation_id=recommendation_id,
    )
    db.add(event)
    db.commit()
    return event


# ---------------------------------------------------------------------------
# Analytics & statistics
# ---------------------------------------------------------------------------

@dataclass
class VariantStats:
    """Aggregated stats for one variant of an experiment."""
    variant: str
    name: str
    impressions: int
    clicks: int
    unique_users: int
    ctr: float  # click-through rate
    confidence: float  # statistical confidence (z-score based)


@dataclass
class ExperimentResults:
    """Full experiment results with winner determination."""
    experiment: dict[str, Any]
    variant_a: VariantStats
    variant_b: VariantStats
    winner: Optional[str]  # "A", "B", or None if inconclusive
    winner_name: str
    lift_pct: float  # relative improvement
    confidence: float
    is_significant: bool  # p < 0.05
    total_events: int
    recommendation: str


def _count_events(db: Session, experiment_id: int, variant: str, event_type: str) -> int:
    return db.execute(
        select(func.count(ABEvent.id)).where(
            ABEvent.experiment_id == experiment_id,
            ABEvent.variant == variant,
            ABEvent.event_type == event_type,
        )
    ).scalar_one()


def _unique_users(db: Session, experiment_id: int, variant: str) -> int:
    return db.execute(
        select(func.count(func.distinct(ABEvent.user_id))).where(
            ABEvent.experiment_id == experiment_id,
            ABEvent.variant == variant,
        )
    ).scalar_one()


def _z_score(ctr_a: float, ctr_b: float, n_a: int, n_b: int) -> float:
    """Compute z-score for difference between two proportions."""
    if n_a == 0 or n_b == 0:
        return 0.0
    p_pool = (ctr_a * n_a + ctr_b * n_b) / (n_a + n_b)
    if p_pool <= 0 or p_pool >= 1:
        return 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return 0.0
    return abs(ctr_a - ctr_b) / se


def _z_to_confidence(z: float) -> float:
    """Approximate z-score to confidence level (percentage)."""
    if z >= 2.576:
        return 99.0
    if z >= 1.96:
        return 95.0
    if z >= 1.645:
        return 90.0
    if z >= 1.282:
        return 80.0
    return round(min(z / 1.96 * 95, 79), 1)


def get_experiment_results(db: Session, experiment_id: int) -> Optional[ExperimentResults]:
    """Compute full results for an experiment."""
    experiment = db.get(ABExperiment, experiment_id)
    if experiment is None:
        return None

    imp_a = _count_events(db, experiment_id, "A", "impression")
    imp_b = _count_events(db, experiment_id, "B", "impression")
    click_a = _count_events(db, experiment_id, "A", "click")
    click_b = _count_events(db, experiment_id, "B", "click")
    users_a = _unique_users(db, experiment_id, "A")
    users_b = _unique_users(db, experiment_id, "B")

    ctr_a = click_a / imp_a if imp_a > 0 else 0.0
    ctr_b = click_b / imp_b if imp_b > 0 else 0.0

    z = _z_score(ctr_a, ctr_b, imp_a, imp_b)
    confidence = _z_to_confidence(z)
    is_significant = z >= 1.96

    if is_significant:
        winner = "A" if ctr_a > ctr_b else "B"
    elif imp_a + imp_b >= 20:
        winner = "A" if ctr_a > ctr_b else ("B" if ctr_b > ctr_a else None)
    else:
        winner = None

    lift = 0.0
    if winner == "A" and ctr_b > 0:
        lift = ((ctr_a - ctr_b) / ctr_b) * 100
    elif winner == "B" and ctr_a > 0:
        lift = ((ctr_b - ctr_a) / ctr_a) * 100

    winner_name = ""
    if winner == "A":
        winner_name = experiment.variant_a_name
    elif winner == "B":
        winner_name = experiment.variant_b_name
    else:
        winner_name = "inconclusive"

    total = imp_a + imp_b + click_a + click_b

    # Generate a recommendation
    if total < 10:
        rec = "Not enough data yet. Keep collecting impressions and clicks."
    elif is_significant:
        rec = f"The '{winner_name}' style is winning with {confidence}% confidence and a {lift:.1f}% lift in CTR. Consider promoting this style as the default."
    elif winner:
        rec = f"The '{winner_name}' style is leading but not yet statistically significant ({confidence}% confidence). Collect more data."
    else:
        rec = "Both variants are performing equally. Continue the experiment."

    return ExperimentResults(
        experiment={
            "id": experiment.id,
            "name": experiment.name,
            "description": experiment.description,
            "is_active": experiment.is_active,
            "created_at": experiment.created_at.isoformat() if experiment.created_at else None,
        },
        variant_a=VariantStats(
            variant="A", name=experiment.variant_a_name,
            impressions=imp_a, clicks=click_a, unique_users=users_a,
            ctr=round(ctr_a * 100, 2), confidence=confidence,
        ),
        variant_b=VariantStats(
            variant="B", name=experiment.variant_b_name,
            impressions=imp_b, clicks=click_b, unique_users=users_b,
            ctr=round(ctr_b * 100, 2), confidence=confidence,
        ),
        winner=winner,
        winner_name=winner_name,
        lift_pct=round(lift, 1),
        confidence=confidence,
        is_significant=is_significant,
        total_events=total,
        recommendation=rec,
    )
