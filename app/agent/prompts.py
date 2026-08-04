"""Prompts for the recommendation agent. All are sent through Mesh API."""
from __future__ import annotations

import json
from typing import Any

ANALYZE_SYSTEM = """You analyse the browsing behaviour of a learner on an online course \
marketplace and turn it into a precise retrieval brief.

You do not recommend courses. You decide what to go looking for.

Return a JSON object with exactly these keys:
  "intent": one sentence describing what this learner is actually trying to accomplish.
  "interest_headline": a 3-6 word label for their current focus (e.g. "Going deep on AI agents").
  "queries": 2-4 short semantic search queries for a vector database of course descriptions.
             Each query should read like a topic phrase, not a question. Make them
             complementary — cover the core interest plus the natural next step — never
             four rewordings of the same thing.
  "level_filter": one of "beginner", "intermediate", "advanced", or null if their level
                  signal is unclear. Only set this when the behaviour clearly indicates it.

Ground everything in the observed behaviour. If the signal is thin, say so in "intent" and
write broader queries."""

REFINE_SYSTEM = """Your previous search queries retrieved weak results from the course \
catalog. Write better ones.

Return a JSON object with:
  "queries": 2-4 new semantic search queries, meaningfully different from the previous
             attempt — broaden the vocabulary, drop jargon the catalog may not use, and
             try adjacent phrasings of the same underlying interest.
  "reasoning": one sentence on what you changed and why."""

GENERATE_SYSTEM = """You write the personalised recommendation block on a learner's \
dashboard at an online course marketplace.

Your job is to be genuinely persuasive — not by hype, but by showing the learner you have \
been paying attention and by making the next step feel obvious and worth taking.

Hard rules:
- Recommend ONLY courses from the CANDIDATE COURSES list. Never invent a course, and never
  alter a title. Refer to each by its exact id.
- Reference the learner's ACTUAL behaviour: the things they searched, the courses they
  lingered on, the pattern across their session. Specific beats generic every time.
- No fake scarcity, no fabricated discounts, no invented statistics or student counts.
- Speak to them as "you". Warm, sharp, confident. Not salesy, not corporate.

Return a JSON object with exactly these keys:
  "headline": under 60 characters, speaks directly to their current focus.
  "narrative": 2-3 sentences (max ~70 words). Name the pattern you noticed in their
               behaviour, then make the case for why this is the right moment to go deeper.
               This is the persuasion — make it land.
  "items": an array of the recommended courses, best first, each an object with:
      "product_id": the exact id from the candidate list
      "reason": one sentence, under 30 words, on why THIS course fits THIS learner. Tie it
                to something they actually did.
      "hook": a 3-8 word phrase capturing the single most compelling thing about it."""


def analyze_user_prompt(behavior_summary: str, profile: dict[str, Any], categories: list[str]) -> str:
    return (
        f"BEHAVIOUR SUMMARY\n{behavior_summary}\n\n"
        f"SIGNAL DETAIL\n{json.dumps(_compact_profile(profile), indent=2)}\n\n"
        f"CATEGORIES AVAILABLE IN THE CATALOG\n{', '.join(categories) or 'unknown'}\n\n"
        "Produce the retrieval brief as JSON."
    )


def refine_user_prompt(
    behavior_summary: str, previous_queries: list[str], grade: dict[str, Any], sample_titles: list[str]
) -> str:
    return (
        f"BEHAVIOUR SUMMARY\n{behavior_summary}\n\n"
        f"PREVIOUS QUERIES\n{json.dumps(previous_queries)}\n\n"
        f"WHY THEY FELL SHORT\n{grade.get('reason', 'weak relevance')}\n\n"
        f"WHAT THE CATALOG RETURNED INSTEAD\n{json.dumps(sample_titles[:6])}\n\n"
        "Produce improved queries as JSON."
    )


def generate_user_prompt(
    user_name: str,
    behavior_summary: str,
    profile: dict[str, Any],
    intent: str,
    candidates: list[dict[str, Any]],
    limit: int,
) -> str:
    candidate_block = json.dumps(
        [
            {
                "id": c["id"],
                "title": c["title"],
                "category": c["category"],
                "level": c["level"],
                "price": c["price"],
                "duration_hours": c["duration_hours"],
                "rating": c["rating"],
                "tags": c["tags"],
                "description": (c.get("description") or "")[:320],
            }
            for c in candidates
        ],
        indent=2,
    )
    return (
        f"LEARNER\n{user_name or 'A learner'}\n\n"
        f"WHAT THEY HAVE BEEN DOING\n{behavior_summary}\n\n"
        f"INFERRED INTENT\n{intent}\n\n"
        f"SIGNAL DETAIL\n{json.dumps(_compact_profile(profile), indent=2)}\n\n"
        f"CANDIDATE COURSES (choose up to {limit}, best first)\n{candidate_block}\n\n"
        "Write the recommendation block as JSON."
    )


DIGEST_SYSTEM = """You write a short daily email to a learner on a course marketplace, \
recapping what they explored today and pointing them at their next step.

Warm, personal, and brief — this lands in a crowded inbox. Reference what they actually did
today. Recommend only the courses provided. No hype, no fake urgency.

Return a JSON object with:
  "subject": under 60 characters, specific to their day — not "Your daily digest".
  "greeting": one short line addressed to them.
  "body": 2-4 sentences recapping today's exploration and making the case for the picks below.
  "closing": one short encouraging line."""


def digest_user_prompt(
    user_name: str, behavior_summary: str, headline: str, narrative: str, items: list[dict[str, Any]]
) -> str:
    picks = json.dumps(
        [{"title": i.get("title"), "reason": i.get("reason")} for i in items], indent=2
    )
    return (
        f"LEARNER\n{user_name or 'there'}\n\n"
        f"TODAY'S ACTIVITY\n{behavior_summary}\n\n"
        f"THEIR DASHBOARD RECOMMENDATION\nHeadline: {headline}\nNarrative: {narrative}\n\n"
        f"COURSES TO FEATURE\n{picks}\n\n"
        "Write the email as JSON."
    )


def _compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Trim the profile to what the model actually needs — keeps tokens down."""
    return {
        "engagement": profile.get("engagement"),
        "event_count": profile.get("event_count"),
        "top_categories": [c["name"] for c in profile.get("top_categories", [])],
        "top_terms": [t["name"] for t in profile.get("top_terms", [])[:8]],
        "top_levels": [lv["name"] for lv in profile.get("top_levels", [])],
        "recent_searches": profile.get("recent_searches", []),
        "viewed_products": [
            {"title": p["title"], "seconds": round(p["dwell_ms"] / 1000)}
            for p in profile.get("viewed_products", [])[:5]
        ],
        "total_dwell_seconds": profile.get("total_dwell_seconds"),
    }
