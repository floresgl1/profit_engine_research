"""UTC helpers. Every timestamp in the system is timezone-aware UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: object, name: str) -> None:
    """Raise unless `value` is a timezone-aware UTC datetime."""
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp (with `Z` or an offset) into UTC."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {text!r}")
    return dt.astimezone(timezone.utc)
