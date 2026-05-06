"""Canonical domain error hierarchy.

All errors raised by adapters and services must be subclasses of DomainError.
The global exception handler in main.py serialises these to RFC 7807 Problem Details.
Provider-specific error formats must never cross the adapter boundary.
"""

from typing import Any


class DomainError(Exception):
    code: str = "internal_error"
    http_status: int = 500
    title: str = "Internal server error"

    def __init__(self, *, detail: str | None = None, extra: dict[str, Any] | None = None) -> None:
        self.detail = detail
        self.extra = extra or {}
        super().__init__(self.title)


# ── Subscription domain ────────────────────────────────────────────────────────

class SubscriptionNotFound(DomainError):
    code = "subscription.not_found"
    http_status = 404
    title = "Subscription not found"


class InvalidICCID(DomainError):
    code = "subscription.invalid_iccid"
    http_status = 400
    title = "Invalid ICCID format"


class PartialResult(DomainError):
    """Raised when a listing can return useful data plus provider failures.

    The router catches this and converts it to a 200 with partial=true.
    """
    code = "subscription.partial_result"
    http_status = 207
    title = "Partial result — some providers failed"


class ListingPreconditionFailed(DomainError):
    """Raised when a listing request cannot be served given the tenant's current state
    or the requested provider's capabilities (e.g. routing map not bootstrapped, or
    provider does not implement company-scoped search).
    """
    code = "subscription.listing_precondition_failed"
    http_status = 412
    title = "Listing precondition failed"


# ── Provider errors (raised by adapters, never exposed raw to the client) ──────

class ProviderUnavailable(DomainError):
    code = "provider.unavailable"
    http_status = 503
    title = "Provider temporarily unavailable"


class ProviderRateLimited(DomainError):
    code = "provider.rate_limited"
    http_status = 429
    title = "Provider rate limit reached"

    def __init__(
        self,
        *,
        detail: str | None = None,
        retry_after: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        merged_extra = extra or {}
        if retry_after is not None:
            merged_extra["retry_after"] = retry_after
        super().__init__(detail=detail, extra=merged_extra)


class ProviderAuthFailed(DomainError):
    code = "provider.auth_failed"
    http_status = 502
    title = "Provider authentication failed"


class ProviderProtocolError(DomainError):
    code = "provider.protocol_error"
    http_status = 502
    title = "Unexpected response from provider"


class ProviderResourceNotFound(DomainError):
    code = "provider.resource_not_found"
    http_status = 404
    title = "Resource not found on provider"

    def __init__(
        self,
        *,
        detail: str | None = None,
        provider_request_id: str | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.provider_request_id = provider_request_id
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        super().__init__(detail=detail, extra=extra)


class ProviderValidationError(DomainError):
    code = "provider.validation_error"
    http_status = 422
    title = "Provider validation error"

    def __init__(
        self,
        *,
        detail: str | None = None,
        provider_request_id: str | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.provider_request_id = provider_request_id
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        super().__init__(detail=detail, extra=extra)


class ProviderForbidden(DomainError):
    code = "provider.forbidden"
    http_status = 403
    title = "Provider forbidden"

    def __init__(
        self,
        *,
        detail: str | None = None,
        provider_request_id: str | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.provider_request_id = provider_request_id
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        super().__init__(detail=detail, extra=extra)


class UnsupportedOperation(DomainError):
    """Raised when an operation is not supported by the SIM's provider."""
    code = "provider.unsupported_operation"
    http_status = 409
    title = "Operation not supported by this provider"


# ── Tenancy / credentials ──────────────────────────────────────────────────────

class CredentialsMissing(DomainError):
    code = "tenant.credentials_missing"
    http_status = 412
    title = "No active provider credentials for this company"


# ── Auth / access ──────────────────────────────────────────────────────────────

class ForbiddenOperation(DomainError):
    code = "auth.forbidden"
    http_status = 403
    title = "Insufficient permissions for this operation"


class IdempotencyKeyRequired(DomainError):
    code = "request.idempotency_key_required"
    http_status = 400
    title = "Idempotency-Key header is required for this operation"
