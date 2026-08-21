"""Pydantic models for clinical notes."""

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.models.base import utcnow


class ReviewStatus(str, Enum):
    """Review state of a note's latest analysis."""

    PENDING = "pending"
    REVIEWED = "reviewed"


def _normalize_pseudonym(value: str | None) -> str | None:
    """Strip optional pseudonyms and reject obvious 9-digit identifiers."""
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    # Reject values that are clearly a bare or dash-separated 9-digit ID.
    # This is not comprehensive PHI detection.
    compact = value.replace("-", "").replace(" ", "")
    if compact.isdigit() and len(compact) == 9:
        raise ValueError("pseudonym cannot be a 9-digit identifier")

    return value


class NoteCreate(BaseModel):
    """Client payload for creating a clinical note."""

    raw_text: str = Field(min_length=1, max_length=20_000)
    pseudonym: str | None = Field(default=None, max_length=100)
    visit_date: date | None = None

    @field_validator("raw_text")
    @classmethod
    def validate_raw_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_text cannot be empty or whitespace only")
        # Preserve original clinical text, including surrounding whitespace.
        return value

    @field_validator("pseudonym")
    @classmethod
    def validate_pseudonym(cls, value: str | None) -> str | None:
        return _normalize_pseudonym(value)


class Note(BaseModel):
    """Clinical note stored in Firestore."""

    id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    raw_text: str = Field(min_length=1, max_length=20_000)
    pseudonym: str | None = Field(default=None, max_length=100)
    visit_date: date | None = None
    created_at: datetime = Field(default_factory=utcnow)

    latest_analysis_id: str | None = Field(default=None, min_length=1)
    analysis_job_id: str | None = Field(default=None, min_length=1)
    review_status: ReviewStatus = ReviewStatus.PENDING
    condition_count: int = Field(default=0, ge=0)

    @field_validator("id", "user_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Identifier cannot be empty or whitespace only")
        return value

    @field_validator("raw_text")
    @classmethod
    def validate_raw_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_text cannot be empty or whitespace only")
        return value

    @field_validator("pseudonym")
    @classmethod
    def validate_pseudonym(cls, value: str | None) -> str | None:
        return _normalize_pseudonym(value)

    @field_validator("latest_analysis_id")
    @classmethod
    def validate_latest_analysis_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("latest_analysis_id cannot be whitespace only")
        return value


class NoteListItem(BaseModel):
    """Lightweight note summary for history views."""

    id: str = Field(min_length=1)
    pseudonym: str | None = Field(default=None, max_length=100)
    visit_date: date | None = None
    created_at: datetime
    review_status: ReviewStatus
    condition_count: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Identifier cannot be empty or whitespace only")
        return value

    @field_validator("pseudonym")
    @classmethod
    def validate_pseudonym(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
