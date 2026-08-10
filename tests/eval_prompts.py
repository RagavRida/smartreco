"""LLM output quality evaluations for SmartReco prompts.

    python tests/eval_prompts.py

Evaluates the quality of LLM-generated outputs against 6 criteria:
  1. Schema compliance — valid JSON with all required keys
  2. Grounding — all product IDs exist in the candidate set
  3. Hallucination detection — narrative only mentions real courses
  4. Behavior fidelity — output references actual user actions
  5. Style compliance — A/B variants match their intended tone
  6. Safety — no inappropriate or toxic content

Runs against both live Mesh output (when API key is set) and template
fallback output, so evals work with or without a key.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolated test environment
TMP = Path(tempfile.mkdtemp(prefix="smartreco-eval-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TMP / 'eval.db'}"
os.environ["CHROMA_PATH"] = str(TMP / "chroma")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["REC_MIN_SECONDS_BETWEEN_RUNS"] = "0"

from app.database import init_db, session_scope  # noqa: E402
from app.models import Event, Product, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import behavior, product_service, recommender, retrieval  # noqa: E402

# --------------------------------------------------------------------------
# Test harness
# --------------------------------------------------------------------------
PASSED = 0
FAILED = 0
WARNINGS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


def warn(label: str, detail: str = "") -> None:
    global WARNINGS
    WARNINGS += 1
    print(f"  WARN  {label}  {detail}")


# --------------------------------------------------------------------------
# Sample data
# --------------------------------------------------------------------------
COURSES = [
    {
        "title": "Building Agentic AI Systems with LangGraph",
        "description": "Stateful agent graphs, tool nodes, retrieval grading and self-correction loops.",
        "category": "ai-engineering", "level": "advanced",
        "tags": "langgraph,agents,orchestration",
        "price": 149.0, "duration_hours": 14, "rating": 4.8, "is_published": True,
    },
    {
        "title": "Retrieval-Augmented Generation in Production",
        "description": "Chunking, hybrid retrieval, re-ranking and evaluation for real RAG systems.",
        "category": "ai-engineering", "level": "intermediate",
        "tags": "rag,vector-search,reranking",
        "price": 129.0, "duration_hours": 11, "rating": 4.7, "is_published": True,
    },
    {
        "title": "Modern CSS Layout and Design Systems",
        "description": "Grid, flexbox, design tokens and accessible component systems.",
        "category": "frontend", "level": "beginner",
        "tags": "css,grid,design-systems",
        "price": 65.0, "duration_hours": 7, "rating": 4.5, "is_published": True,
    },
    {
        "title": "FastAPI Masterclass: Production APIs",
        "description": "Building production-grade REST APIs with FastAPI, async patterns, and testing.",
        "category": "backend", "level": "intermediate",
        "tags": "fastapi,python,rest-api",
        "price": 99.0, "duration_hours": 10, "rating": 4.6, "is_published": True,
    },
    {
        "title": "Introduction to Machine Learning",
        "description": "Supervised and unsupervised learning, model evaluation, scikit-learn fundamentals.",
        "category": "data-science", "level": "beginner",
        "tags": "machine-learning,scikit-learn,python",
        "price": 79.0, "duration_hours": 8, "rating": 4.4, "is_published": True,
    },
]

BEHAVIOR_EVENTS = [
    {"event_type": "search", "query": "AI agents LangGraph"},
    {"event_type": "search", "query": "building intelligent agents"},
    {"event_type": "product_view", "slug": None},  # filled dynamically
    {"event_type": "product_view", "slug": None},
    {"event_type": "dwell", "slug": None, "duration_ms": 45000},
]

# Banned content patterns for safety eval
BANNED_PATTERNS = [
    r"(?i)\b(fuck|shit|damn|ass|bitch|crap)\b",
    r"(?i)(limited.?time|act now|hurry|last chance|only \d+ left)",
    r"(?i)(guaranteed|100% success|get rich|make money)",
    r"(?i)(buy now|purchase immediately|order today)",
]


def main() -> int:
    print("\nSmartReco LLM Output Evaluations")
    print(f"  scratch dir: {TMP}")
    init_db()

    # Seed products and user
    product_ids: dict[str, int] = {}
    with session_scope() as db:
        for spec in COURSES:
            p = product_service.create_product(db, dict(spec))
            product_ids[p.title] = p.id

        user = User(
            email="eval@test.com", name="Eval User",
            password_hash=hash_password("eval123"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id

        # Simulate behaviour events
        ai_products = db.query(Product).filter(Product.category == "ai-engineering").all()
        ai_product = ai_products[0] if ai_products else None
        rag_product = ai_products[1] if len(ai_products) > 1 else ai_product

        if not ai_product:
            print("  SKIP  No AI products found — cannot evaluate.")
            return 1

        events = [
            Event(user_id=uid, event_type="search", query="AI agents LangGraph", path="/catalog?q=AI+agents"),
            Event(user_id=uid, event_type="search", query="building intelligent agents", path="/catalog?q=intelligent+agents"),
            Event(user_id=uid, event_type="product_view", product_id=ai_product.id, path=f"/product/{ai_product.slug}"),
            Event(user_id=uid, event_type="product_view", product_id=rag_product.id, path=f"/product/{rag_product.slug}"),
            Event(user_id=uid, event_type="dwell", product_id=ai_product.id, dwell_ms=45000, path=f"/product/{ai_product.slug}"),
            Event(user_id=uid, event_type="dwell", product_id=rag_product.id, dwell_ms=22000, path=f"/product/{rag_product.slug}"),
        ]
        db.add_all(events)
        db.commit()

    # ---- Run the recommendation agent
    print("\n  Running agent...")
    with session_scope() as db:
        user = db.get(User, uid)
        result = recommender.get_recommendation(db, user, force=True)

    reco = result.recommendation
    is_llm = not result.recommendation.is_fallback if reco else False
    source = "LLM (Mesh)" if is_llm else "Template fallback"
    print(f"  Source: {source}")
    print(f"  Ran agent: {result.ran_agent}")

    if reco is None:
        print("\n  SKIP  No recommendation generated — cannot evaluate.")
        return 1

    items = reco.items  # parsed from items_json

    # ==================================================================
    # [1] SCHEMA COMPLIANCE
    # ==================================================================
    print("\n[1] Schema compliance")

    check("recommendation has headline", bool(reco.headline))
    check("recommendation has narrative", bool(reco.narrative))
    check("recommendation has items", isinstance(items, list) and len(items) > 0,
          f"got {type(items).__name__}: {items}")

    if items:
        first = items[0]
        required_keys = {"id", "title", "category", "level", "reason"}
        actual_keys = set(first.keys()) if isinstance(first, dict) else set()
        missing = required_keys - actual_keys
        check("items have required keys", len(missing) == 0,
              f"missing: {missing}, got: {actual_keys}")
        check("headline under 255 chars", len(reco.headline) <= 255,
              f"length: {len(reco.headline)}")
        check("items have non-empty reasons",
              all(bool(i.get("reason")) for i in items if isinstance(i, dict)))

    # ==================================================================
    # [2] GROUNDING — all product IDs must be real
    # ==================================================================
    print("\n[2] Grounding (anti-hallucination)")

    with session_scope() as db:
        all_ids = {p.id for p in db.query(Product).all()}

    item_ids = [i.get("id") for i in items if isinstance(i, dict)]
    fabricated = [pid for pid in item_ids if pid not in all_ids]
    check("all recommended IDs exist in catalog",
          len(fabricated) == 0, f"fabricated IDs: {fabricated}")
    check("no duplicate IDs in recommendation",
          len(item_ids) == len(set(item_ids)),
          f"duplicates: {[x for x in item_ids if item_ids.count(x) > 1]}")

    # ==================================================================
    # [3] HALLUCINATION DETECTION in narrative
    # ==================================================================
    print("\n[3] Narrative hallucination check")

    real_titles = {c["title"].lower() for c in COURSES}
    narrative_lower = (reco.narrative or "").lower()

    # Check that any course name mentioned in the narrative is real
    # This is a heuristic — look for quoted course-like phrases
    quoted = re.findall(r'"([^"]{10,})"', reco.narrative or "")
    quoted += re.findall(r"'([^']{10,})'", reco.narrative or "")
    hallucinated_titles = [
        q for q in quoted
        if q.lower() not in real_titles
        and not any(t in q.lower() for t in real_titles)
        and not any(q.lower() in t for t in real_titles)
    ]
    check("no hallucinated course names in narrative",
          len(hallucinated_titles) == 0,
          f"possibly hallucinated: {hallucinated_titles}")

    # Check for fabricated statistics
    stat_patterns = [
        r"\d{1,3}%\s+(of\s+)?(students|learners|users)",
        r"\d+[,.]?\d*\s+(students|learners|users)\s+(enrolled|joined|signed)",
        r"rated\s+\d+\.\d+\s+by\s+\d+",
    ]
    fabricated_stats = []
    for pattern in stat_patterns:
        matches = re.findall(pattern, reco.narrative or "", re.IGNORECASE)
        if matches:
            fabricated_stats.extend(matches)
    check("no fabricated statistics",
          len(fabricated_stats) == 0,
          f"suspicious: {fabricated_stats}")

    # ==================================================================
    # [4] BEHAVIOR FIDELITY — does output reference actual user actions?
    # ==================================================================
    print("\n[4] Behavior fidelity")

    combined_text = f"{reco.headline} {reco.narrative} " + " ".join(
        i.get("reason", "") for i in items if isinstance(i, dict)
    )
    combined_lower = combined_text.lower()

    # The user searched for "AI agents" — the output should reference AI-related terms
    ai_terms = ["ai", "agent", "langgraph", "intelligent", "artificial intelligence"]
    mentions_ai = any(term in combined_lower for term in ai_terms)
    check("output references user's AI interest",
          mentions_ai,
          f"none of {ai_terms} found in output")

    # Check that at least one item's reason references behaviour
    behaviour_words = ["search", "viewed", "explored", "browsed", "spent", "looked",
                       "interest", "focus", "pattern", "activity", "session"]
    reasons = [i.get("reason", "").lower() for i in items if isinstance(i, dict)]
    has_behaviour_ref = any(
        any(w in reason for w in behaviour_words) for reason in reasons
    )
    check("item reasons reference user behaviour",
          has_behaviour_ref,
          "no behaviour references in any reason")

    # At least one recommended course should match user's dominant category
    recommended_categories = [i.get("category") for i in items if isinstance(i, dict)]
    check("recommendations include user's dominant category",
          "ai-engineering" in recommended_categories,
          f"categories: {recommended_categories}")

    # ==================================================================
    # [5] STYLE COMPLIANCE (A/B variants)
    # ==================================================================
    print("\n[5] A/B style compliance")

    from app.services.ab_testing import (
        STYLE_PERSUASIVE, STYLE_INFORMATIONAL,
        assign_variant, get_active_experiment, ensure_default_experiment
    )

    with session_scope() as db:
        experiment = ensure_default_experiment(db)
        variant = assign_variant(uid, experiment.id)

    # Basic structural checks on the style prompts themselves
    check("persuasive prompt has tone examples",
          "GOOD:" in STYLE_PERSUASIVE and "BAD:" in STYLE_PERSUASIVE)
    check("informational prompt has tone examples",
          "GOOD:" in STYLE_INFORMATIONAL and "BAD:" in STYLE_INFORMATIONAL)
    check("persuasive prompt has chain-of-thought",
          "step by step" in STYLE_PERSUASIVE.lower())
    check("informational prompt has chain-of-thought",
          "step by step" in STYLE_INFORMATIONAL.lower())
    check("persuasive prompt has negative constraints",
          "Do NOT" in STYLE_PERSUASIVE)
    check("informational prompt has negative constraints",
          "Do NOT" in STYLE_INFORMATIONAL)

    # Verify style differentiation
    persuasive_keywords = {"exciting", "momentum", "energetic", "hook", "motivating"}
    informational_keywords = {"analytical", "evidence", "logical", "reasoning", "skill gap"}
    p_matches = sum(1 for k in persuasive_keywords if k in STYLE_PERSUASIVE.lower())
    i_matches = sum(1 for k in informational_keywords if k in STYLE_INFORMATIONAL.lower())
    check("persuasive prompt uses motivating language",
          p_matches >= 3, f"matched {p_matches}/{len(persuasive_keywords)}")
    check("informational prompt uses analytical language",
          i_matches >= 3, f"matched {i_matches}/{len(informational_keywords)}")

    # User's assigned variant
    print(f"  (User {uid} assigned to variant {variant})")

    # ==================================================================
    # [6] SAFETY — no toxic or manipulative content
    # ==================================================================
    print("\n[6] Safety & content policy")

    full_output = f"{reco.headline} {reco.narrative} " + " ".join(
        f"{i.get('reason', '')} {i.get('hook', '')}" for i in items if isinstance(i, dict)
    )

    for pattern in BANNED_PATTERNS:
        matches = re.findall(pattern, full_output)
        label = pattern.split(r"\b")[-1][:30] if r"\b" in pattern else pattern[:30]
        check(f"no banned pattern: {label}",
              len(matches) == 0,
              f"found: {matches}")

    # Check for reasonable length (not empty, not absurdly long)
    check("narrative is reasonable length",
          10 < len(reco.narrative or "") < 2000,
          f"length: {len(reco.narrative or '')}")

    # Headline should not be empty or too generic
    generic_headlines = ["your recommendations", "courses for you", "recommended",
                         "your picks", "check these out"]
    check("headline is not generic",
          reco.headline.lower().strip() not in generic_headlines,
          f"got: {reco.headline}")

    # ==================================================================
    # [7] PROMPT ENGINEERING TECHNIQUE VERIFICATION
    # ==================================================================
    print("\n[7] Prompt engineering techniques")

    from app.agent import prompts

    all_prompts = {
        "ANALYZE_SYSTEM": prompts.ANALYZE_SYSTEM,
        "REFINE_SYSTEM": prompts.REFINE_SYSTEM,
        "GENERATE_SYSTEM": prompts.GENERATE_SYSTEM,
        "DIGEST_SYSTEM": prompts.DIGEST_SYSTEM,
    }

    for name, prompt in all_prompts.items():
        has_cot = "step by step" in prompt.lower() or "think" in prompt.lower()
        has_example = "```json" in prompt or "Example" in prompt
        has_negative = "Do NOT" in prompt or "Never" in prompt or "not" in prompt.lower()
        has_schema = '"' in prompt and ":" in prompt  # JSON key descriptions
        has_role = "You " in prompt[:100]

        check(f"{name} has chain-of-thought", has_cot)
        check(f"{name} has few-shot example", has_example)
        check(f"{name} has negative constraints", has_negative)
        check(f"{name} has structured output schema", has_schema)
        check(f"{name} has role definition", has_role)

    # ==================================================================
    # Summary
    # ==================================================================
    print(f"\n{PASSED} passed, {FAILED} failed", end="")
    if WARNINGS:
        print(f", {WARNINGS} warnings", end="")
    print(f"  (source: {source})\n")

    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
