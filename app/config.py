"""Central configuration. Everything is read from the environment (.env)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """Minimal .env loader (avoids a hard dependency on python-dotenv)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Application settings. Instantiated once via get_settings()."""

    # --- Core ---
    app_name: str = "SmartReco"
    secret_key: str = _env("SECRET_KEY", "dev-secret-change-me")
    database_url: str = _env("DATABASE_URL", f"sqlite:///{DATA_DIR / 'smartreco.db'}")

    # --- JWT Authentication ---
    jwt_secret_key: str = _env("JWT_SECRET_KEY", "") or _env("SECRET_KEY", "dev-secret-change-me")
    jwt_algorithm: str = _env("JWT_ALGORITHM", "HS256")
    jwt_access_token_expire_minutes: int = _env_int("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 30)
    jwt_refresh_token_expire_days: int = _env_int("JWT_REFRESH_TOKEN_EXPIRE_DAYS", 7)

    # --- Mesh API (mandatory for all LLM calls) ---
    mesh_api_key: str = _env("MESH_API_KEY", "")
    mesh_base_url: str = _env("MESH_BASE_URL", "https://api.meshapi.ai/v1")
    mesh_chat_model: str = _env("MESH_CHAT_MODEL", "openai/gpt-4o-mini")
    mesh_reasoning_model: str = _env("MESH_REASONING_MODEL", "openai/gpt-4o")
    mesh_embedding_model: str = _env("MESH_EMBEDDING_MODEL", "openai/text-embedding-3-small")
    mesh_timeout: int = _env_int("MESH_TIMEOUT_SECONDS", 45)
    mesh_max_retries: int = _env_int("MESH_MAX_RETRIES", 2)

    # --- Vector store ---
    chroma_path: str = _env("CHROMA_PATH", str(DATA_DIR / "chroma"))
    chroma_collection: str = _env("CHROMA_COLLECTION", "smartreco_products")
    embedding_dim: int = _env_int("EMBEDDING_DIM", 512)  # used by the offline fallback

    # --- Recommendation engine economics ---
    # The agent is expensive: it only runs when the trigger policy says it is worth it.
    rec_min_new_events: int = _env_int("REC_MIN_NEW_EVENTS", 5)
    rec_cache_ttl_seconds: int = _env_int("REC_CACHE_TTL_SECONDS", 1800)
    rec_min_seconds_between_runs: int = _env_int("REC_MIN_SECONDS_BETWEEN_RUNS", 120)
    rec_products_returned: int = _env_int("REC_PRODUCTS_RETURNED", 4)
    rec_retrieval_k: int = _env_int("REC_RETRIEVAL_K", 12)
    rec_max_refine_loops: int = _env_int("REC_MAX_REFINE_LOOPS", 2)
    behavior_window_events: int = _env_int("BEHAVIOR_WINDOW_EVENTS", 120)
    behavior_window_hours: int = _env_int("BEHAVIOR_WINDOW_HOURS", 336)

    # --- Event ingestion ---
    events_max_batch: int = _env_int("EVENTS_MAX_BATCH", 100)

    # --- Scheduler (bonus) ---
    enable_scheduler: bool = _env_bool("ENABLE_SCHEDULER", True)
    digest_hour: int = _env_int("DIGEST_HOUR", 16)
    digest_minute: int = _env_int("DIGEST_MINUTE", 0)
    digest_timezone: str = _env("DIGEST_TIMEZONE", "UTC")

    # --- Email delivery (bonus) ---
    smtp_host: str = _env("SMTP_HOST", "")
    smtp_port: int = _env_int("SMTP_PORT", 587)
    smtp_user: str = _env("SMTP_USER", "")
    smtp_password: str = _env("SMTP_PASSWORD", "")
    smtp_from: str = _env("SMTP_FROM", "SmartReco <noreply@smartreco.local>")
    smtp_use_tls: bool = _env_bool("SMTP_USE_TLS", True)

    # --- Observability (bonus) ---
    langsmith_api_key: str = _env("LANGCHAIN_API_KEY", "") or _env("LANGSMITH_API_KEY", "")
    langsmith_project: str = _env("LANGCHAIN_PROJECT", "smartreco")
    langsmith_tracing: bool = _env_bool("LANGCHAIN_TRACING_V2", False)

    @property
    def mesh_configured(self) -> bool:
        return bool(self.mesh_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
