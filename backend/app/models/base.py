"""Shared helpers for model definitions."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time for model default factories."""
    return datetime.now(timezone.utc)
