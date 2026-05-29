"""Opaque pagination-cursor codecs (base64url-encoded JSON maps)."""
import base64
import json

_GLOBAL_CURSOR_PREFIX = "global:"
_ADMIN_CURSOR_PREFIX = "admin:"
_STATUS_CURSOR_PREFIX = "statuses:"


def encode_cursor(prefix: str, mapping: dict[str, str | None]) -> str | None:
    active = {k: v for k, v in mapping.items() if v is not None}
    if not active:
        return None
    payload = json.dumps(active, separators=(",", ":"), sort_keys=True).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{prefix}{token}"


def decode_cursor(prefix: str, cursor: str | None) -> dict[str, str | None] | None:
    if cursor is None or not cursor.startswith(prefix):
        return None
    token = cursor[len(prefix):]
    padded = token + ("=" * (-len(token) % 4))
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): (str(v) if v is not None else None) for k, v in decoded.items()}
