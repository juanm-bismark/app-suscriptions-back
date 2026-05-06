from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CredentialExpiryStatus(StrEnum):
    VALID = "valid"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    INVALID = "invalid"


_EXPIRY_KEYS = (
    "cert_expires_at",
    "token_expires_at",
    "expires_at",
)


def credential_expiry_status(
    account_scope: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> CredentialExpiryStatus:
    scope = account_scope or {}
    raw = next((scope[key] for key in _EXPIRY_KEYS if scope.get(key)), None)
    if raw is None:
        return CredentialExpiryStatus.VALID

    try:
        expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return CredentialExpiryStatus.INVALID

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    days_remaining = (expires_at - reference).days
    if days_remaining < 0:
        return CredentialExpiryStatus.EXPIRED
    if days_remaining <= 30:
        return CredentialExpiryStatus.EXPIRING
    return CredentialExpiryStatus.VALID


def credential_expiry_datetime(
    account_scope: dict[str, Any] | None,
) -> datetime | None:
    scope = account_scope or {}
    raw = next((scope[key] for key in _EXPIRY_KEYS if scope.get(key)), None)
    if raw is None:
        return None
    try:
        expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at
