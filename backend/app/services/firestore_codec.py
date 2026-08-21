"""Firestore document serialization and parsing helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 100


class DocumentDataError(ValueError):
    """Raised when a stored document cannot be parsed into a domain model."""


class DocumentConflictError(RuntimeError):
    """Raised when a create-only write targets an existing document."""


def clamp_history_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an int")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, _MAX_HISTORY_LIMIT)


def to_firestore(value: Any) -> Any:
    """Convert Python/Pydantic values into Firestore-safe types.

    Enums, tuples, nested models, and plain ``date`` values are not accepted by
    Firestore. Timezone-aware datetimes are preserved for Timestamp ordering.
    """
    if isinstance(value, BaseModel):
        return to_firestore(value.model_dump(mode="python"))

    if isinstance(value, dict):
        return {key: to_firestore(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [to_firestore(item) for item in value]

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, date):
        return value.isoformat()

    return value


def dump_document(model: BaseModel, *, exclude: set[str]) -> dict[str, Any]:
    payload = model.model_dump(mode="python", exclude=exclude)
    serialized = to_firestore(payload)
    if not isinstance(serialized, dict):
        raise TypeError("Firestore document payload must be a dict")
    return serialized


def parse_model(model_type: type[BaseModel], doc_id: str, data: dict[str, Any]) -> Any:
    try:
        return model_type(id=doc_id, **data)
    except ValidationError as exc:
        raise DocumentDataError(
            f"Invalid {model_type.__name__} document '{doc_id}'"
        ) from exc


def should_mark_note_reviewed_for_analysis(
    note_data: dict[str, Any],
    analysis_id: str,
) -> bool:
    """True only when the reviewed analysis is still the note's latest."""
    return note_data.get("latest_analysis_id") == analysis_id


def is_already_exists(exc: Exception) -> bool:
    """Detect Firestore already-exists failures across SDK variants."""
    name = type(exc).__name__
    if name in {"AlreadyExists", "Conflict"}:
        return True
    code = getattr(exc, "code", None)
    if code is not None and str(code).endswith("ALREADY_EXISTS"):
        return True
    return "already exists" in str(exc).lower()


def review_document_id(analysis_id: str) -> str:
    """Deterministic review document ID: one review per analysis."""
    analysis_id = analysis_id.strip()
    if not analysis_id:
        raise ValueError("analysis_id cannot be empty")
    return analysis_id


def validate_condition_count(condition_count: int) -> None:
    if isinstance(condition_count, bool) or not isinstance(condition_count, int):
        raise TypeError("condition_count must be an int")
    if condition_count < 0:
        raise ValueError("condition_count cannot be negative")
