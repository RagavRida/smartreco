"""Seed the catalog and demo accounts.

    python -m app.seed              # seed products + accounts
    python -m app.seed --demo       # also simulate a browsing session for the demo user
    python -m app.seed --reset      # wipe and reseed
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from .config import settings
from .database import init_db, session_scope
from .models import AgentState, Event, Product, Recommendation, User
from .security import hash_password
from .services import product_service

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("seed")

ADMIN = {"email": "admin@smartreco.dev", "password": "admin123", "name": "Admin", "role": "admin"}
LEARNER = {"email": "learner@smartreco.dev", "password": "learner123", "name": "Ada", "role": "user"}

CATALOG = [
    # --- ai-engineering ---
    {
        "title": "Building Agentic AI Systems with LangGraph",
        "category": "ai-engineering", "level": "advanced", "price": 149.0, "duration_hours": 14,
        "instructor": "Dr. Priya Raman", "rating": 4.8,
        "tags": "langgraph, agents, orchestration, tool-use, state-machines",
        "description": (
            "Design production agents as explicit graphs instead of prompt spaghetti. You will build "
            "stateful workflows with conditional routing, tool-calling nodes, retrieval grading and "
            "bounded self-correction loops, then instrument the whole thing with tracing so you can "
            "actually debug what your agent decided and why."
        ),
    },
    {
        "title": "Retrieval-Augmented Generation in Production",
        "category": "ai-engineering", "level": "intermediate", "price": 129.0, "duration_hours": 11,
        "instructor": "Marcus Feld", "rating": 4.7,
        "tags": "rag, vector-search, embeddings, chunking, reranking",
        "description": (
            "Move past the toy RAG demo. Covers chunking strategies that survive real documents, "
            "hybrid retrieval, cross-encoder re-ranking, metadata filtering, evaluation harnesses, "
            "and the failure modes that quietly wreck answer quality at scale."
        ),
    },
    {
        "title": "Prompt Engineering for Reliable Systems",
        "category": "ai-engineering", "level": "beginner", "price": 59.0, "duration_hours": 6,
        "instructor": "Lena Ortiz", "rating": 4.5,
        "tags": "prompting, structured-output, json-mode, evaluation",
        "description": (
            "Prompting as engineering, not incantation. Structured outputs, schema validation, "
            "few-shot design, failure taxonomies, and building regression tests so a model upgrade "
            "does not silently break your product."
        ),
    },
    {
        "title": "Vector Databases Deep Dive: Chroma, Pinecone & Qdrant",
        "category": "ai-engineering", "level": "intermediate", "price": 99.0, "duration_hours": 8,
        "instructor": "Dr. Priya Raman", "rating": 4.6,
        "tags": "vector-database, chroma, qdrant, hnsw, indexing, embeddings",
        "description": (
            "How vector stores actually work: HNSW and IVF indexes, distance metrics, filtering "
            "strategies, sharding, and the operational realities of keeping a vector index in sync "
            "with your system of record."
        ),
    },
    {
        "title": "LLM Observability and Evaluation",
        "category": "ai-engineering", "level": "advanced", "price": 119.0, "duration_hours": 9,
        "instructor": "Sofia Bergman", "rating": 4.7,
        "tags": "langsmith, tracing, evaluation, monitoring, llm-ops",
        "description": (
            "You cannot improve what you cannot see. Instrument multi-step agents with tracing, "
            "build offline and online eval suites, catch regressions before users do, and put a "
            "cost and latency budget around every model call."
        ),
    },
    # --- backend ---
    {
        "title": "FastAPI in Production",
        "category": "backend", "level": "intermediate", "price": 89.0, "duration_hours": 10,
        "instructor": "Tomás Ruiz", "rating": 4.8,
        "tags": "fastapi, python, async, api-design, deployment",
        "description": (
            "Take FastAPI from tutorial to production: dependency design, async pitfalls, background "
            "tasks, structured logging, migrations, auth, containerisation and load-testing under "
            "real traffic shapes."
        ),
    },
    {
        "title": "Event-Driven Architecture with Python",
        "category": "backend", "level": "advanced", "price": 139.0, "duration_hours": 13,
        "instructor": "Tomás Ruiz", "rating": 4.6,
        "tags": "events, queues, celery, kafka, batching, throughput",
        "description": (
            "Build systems that absorb bursty load without falling over. Batching, backpressure, "
            "idempotency, at-least-once semantics, dead-letter handling, and choosing between "
            "queues, streams and plain background jobs."
        ),
    },
    {
        "title": "Database Design for High-Write Workloads",
        "category": "backend", "level": "intermediate", "price": 109.0, "duration_hours": 9,
        "instructor": "Nadia Cole", "rating": 4.5,
        "tags": "postgres, sqlite, indexing, schema-design, write-throughput",
        "description": (
            "Schema and index design when writes dominate: append-only tables, partitioning, "
            "batched inserts, WAL tuning, and the read patterns worth denormalising for."
        ),
    },
    # --- data ---
    {
        "title": "Analytics Engineering with dbt and SQL",
        "category": "data", "level": "beginner", "price": 79.0, "duration_hours": 12,
        "instructor": "Nadia Cole", "rating": 4.4,
        "tags": "sql, dbt, modelling, warehouse, analytics",
        "description": (
            "Turn raw event tables into models people trust. Dimensional modelling, incremental "
            "builds, testing and documentation as a first-class part of the pipeline."
        ),
    },
    {
        "title": "Behavioural Analytics: Turning Clickstreams into Decisions",
        "category": "data", "level": "intermediate", "price": 95.0, "duration_hours": 8,
        "instructor": "Sofia Bergman", "rating": 4.6,
        "tags": "clickstream, tracking, funnels, cohorts, personalisation",
        "description": (
            "Instrument a product properly, then actually use the data: sessionisation, funnels, "
            "cohort retention, interest modelling, and feeding behavioural signal into "
            "personalisation without drowning in noise."
        ),
    },
    # --- frontend ---
    {
        "title": "Performance-First Frontend JavaScript",
        "category": "frontend", "level": "intermediate", "price": 85.0, "duration_hours": 9,
        "instructor": "Lena Ortiz", "rating": 4.7,
        "tags": "javascript, performance, throttling, web-vitals, instrumentation",
        "description": (
            "Ship instrumentation and rich interaction without tanking Core Web Vitals. Event loop "
            "budgets, throttling and debouncing, requestIdleCallback, sendBeacon, and measuring the "
            "cost of your own analytics."
        ),
    },
    {
        "title": "Modern CSS Layout and Design Systems",
        "category": "frontend", "level": "beginner", "price": 65.0, "duration_hours": 7,
        "instructor": "Ines Duarte", "rating": 4.5,
        "tags": "css, grid, design-systems, accessibility, components",
        "description": (
            "Grid and flexbox properly, design tokens, accessible components, and a system that "
            "stays coherent as the product grows."
        ),
    },
    # --- product / business ---
    {
        "title": "Persuasive Product Copywriting",
        "category": "product", "level": "beginner", "price": 55.0, "duration_hours": 5,
        "instructor": "Ines Duarte", "rating": 4.3,
        "tags": "copywriting, conversion, messaging, positioning",
        "description": (
            "Write copy that moves people without manipulating them. Positioning, specificity over "
            "adjectives, objection handling, and testing your way to messaging that converts."
        ),
    },
    {
        "title": "Recommender Systems: From Collaborative Filtering to LLMs",
        "category": "machine-learning", "level": "advanced", "price": 159.0, "duration_hours": 16,
        "instructor": "Dr. Priya Raman", "rating": 4.9,
        "tags": "recommenders, collaborative-filtering, ranking, embeddings, personalisation",
        "description": (
            "The full arc of recommendation: matrix factorisation, two-tower retrieval, learning to "
            "rank, cold-start strategies, and where LLM-based reasoning genuinely beats classical "
            "approaches — and where it does not."
        ),
    },
    {
        "title": "Python for Data Analysis Foundations",
        "category": "data", "level": "beginner", "price": 49.0, "duration_hours": 10,
        "instructor": "Marcus Feld", "rating": 4.4,
        "tags": "python, pandas, numpy, visualisation, statistics",
        "description": (
            "A solid on-ramp: pandas fundamentals, cleaning messy data, exploratory analysis and "
            "communicating results with clear visualisations."
        ),
    },
    {
        "title": "MLOps: Shipping and Monitoring Models",
        "category": "machine-learning", "level": "intermediate", "price": 125.0, "duration_hours": 12,
        "instructor": "Sofia Bergman", "rating": 4.5,
        "tags": "mlops, deployment, monitoring, drift, ci-cd",
        "description": (
            "Get models out of notebooks: packaging, reproducible training, deployment patterns, "
            "drift detection and the monitoring that tells you when to retrain."
        ),
    },
]

# A scripted session for the demo learner: someone converging on agentic AI.
DEMO_SESSION = [
    ("search", {"query": "ai agents"}),
    ("page_view", {"path": "/catalog"}),
    ("product_view", {"title": "Building Agentic AI Systems with LangGraph", "dwell_ms": 94000}),
    ("click", {"title": "Building Agentic AI Systems with LangGraph"}),
    ("scroll_depth", {"title": "Building Agentic AI Systems with LangGraph", "value": 75}),
    ("search", {"query": "langgraph agent workflow"}),
    ("product_view", {"title": "Retrieval-Augmented Generation in Production", "dwell_ms": 71000}),
    ("scroll_depth", {"title": "Retrieval-Augmented Generation in Production", "value": 50}),
    ("product_view", {"title": "Vector Databases Deep Dive: Chroma, Pinecone & Qdrant", "dwell_ms": 46000}),
    ("search", {"query": "rag retrieval quality"}),
    ("product_view", {"title": "LLM Observability and Evaluation", "dwell_ms": 38000}),
    ("click", {"title": "LLM Observability and Evaluation"}),
    ("page_view", {"path": "/catalog?category=ai-engineering"}),
    ("product_view", {"title": "Building Agentic AI Systems with LangGraph", "dwell_ms": 52000}),
]


def _reset() -> None:
    logger.info("Resetting database and vector store…")
    with session_scope() as db:
        for model in (Event, Recommendation, AgentState, Product, User):
            db.execute(delete(model))
    chroma_dir = Path(settings.chroma_path)
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir, ignore_errors=True)


def seed_users() -> None:
    with session_scope() as db:
        for spec in (ADMIN, LEARNER):
            existing = db.execute(select(User).where(User.email == spec["email"])).scalar_one_or_none()
            if existing:
                continue
            db.add(
                User(
                    email=spec["email"],
                    name=spec["name"],
                    role=spec["role"],
                    password_hash=hash_password(spec["password"]),
                )
            )
            logger.info("Created %s (%s / %s)", spec["role"], spec["email"], spec["password"])


def seed_products() -> None:
    with session_scope() as db:
        created = 0
        for spec in CATALOG:
            exists = db.execute(select(Product).where(Product.title == spec["title"])).scalar_one_or_none()
            if exists:
                continue
            payload = dict(spec)
            payload["is_published"] = True
            product_service.create_product(db, payload)
            created += 1
        logger.info("Seeded %s products (dual-written to SQL + vector store)", created)
        status = product_service.sync_status(db)
        logger.info("Sync status: %s", status)


def seed_demo_activity() -> None:
    """Write a realistic browsing session so the agent has something to reason over."""
    with session_scope() as db:
        user = db.execute(select(User).where(User.email == LEARNER["email"])).scalar_one_or_none()
        if user is None:
            logger.warning("Demo learner missing — run without --demo first")
            return
        existing = db.execute(
            select(func.count(Event.id)).where(Event.user_id == user.id)
        ).scalar_one()
        if existing:
            logger.info("Demo learner already has %s events — skipping", existing)
            return

        by_title = {p.title: p for p in db.execute(select(Product)).scalars().all()}
        now = datetime.utcnow()
        session_id = "demo-session"
        rows = []

        for index, (event_type, spec) in enumerate(DEMO_SESSION):
            product = by_title.get(spec.get("title", ""))
            created = now - timedelta(minutes=(len(DEMO_SESSION) - index) * 4 + random.randint(0, 3))
            rows.append(
                Event(
                    user_id=user.id,
                    session_id=session_id,
                    anon_id="demo-anon",
                    event_type=event_type,
                    product_id=product.id if product else None,
                    category=product.category if product else "",
                    query=spec.get("query", ""),
                    path=spec.get("path", f"/product/{product.slug}" if product else "/"),
                    dwell_ms=int(spec.get("dwell_ms", 0)),
                    value=float(spec.get("value", 0)),
                    meta_json=json.dumps(
                        {"level": product.level, "tags": product.tag_list} if product else {}
                    ),
                    client_ts=created,
                    created_at=created,
                )
            )
        db.bulk_save_objects(rows)
        logger.info("Seeded %s demo events for %s", len(rows), user.email)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed SmartReco")
    parser.add_argument("--reset", action="store_true", help="wipe existing data first")
    parser.add_argument("--demo", action="store_true", help="also seed a demo browsing session")
    args = parser.parse_args()

    init_db()
    if args.reset:
        _reset()
    seed_users()
    seed_products()
    if args.demo:
        seed_demo_activity()
    logger.info("Done. Start the app with: uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
