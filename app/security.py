"""Password hashing + signed session cookies. Deliberately simple auth."""
from __future__ import annotations

from typing import Optional

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

SESSION_COOKIE = "smartreco_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="smartreco-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def make_session(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session(token: str) -> Optional[int]:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None
