"""Password hashing, signed session cookies, and JWT tokens."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

# ---------------------------------------------------------------------------
# Session cookies (kept for browser-rendered HTML pages)
# ---------------------------------------------------------------------------
SESSION_COOKIE = "smartreco_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="smartreco-session")


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def make_session(user_id: int) -> str:
    """Create a signed session cookie value."""
    return _serializer.dumps({"uid": user_id})


def read_session(token: str) -> Optional[int]:
    """Read and validate a signed session cookie, returning the user_id or None."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


# ---------------------------------------------------------------------------
# JWT tokens (for programmatic API access and role-based authorization)
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, role: str) -> str:
    """Create a short-lived JWT access token embedding the user's id and role."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    """Create a longer-lived JWT refresh token (used only to mint new access tokens)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate a JWT token. Returns the payload dict or None on failure."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
