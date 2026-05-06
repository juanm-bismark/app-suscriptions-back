import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

REFRESH_TOKEN_EXPIRE_DAYS = 5


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str, secret: str, expire_minutes: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload: dict[str, str | datetime] = {"sub": subject, "exp": expire}
    return jwt.encode(payload, secret, algorithm="HS256")


def generate_refresh_token() -> tuple[str, datetime]:
    """Return (raw_token, expires_at). The raw token is sent to the client."""
    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return token, expires_at


def hash_refresh_token(raw: str) -> str:
    """sha256 hex digest — what gets stored in the DB, never the raw value."""
    return hashlib.sha256(raw.encode()).hexdigest()
