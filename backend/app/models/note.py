"""Pydantic models for clinical notes."""

import re

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.models.base import utcnow


class ReviewStatus(str, Enum):
    """Review state of a note's latest analysis."""

    PENDING = "pending"
    REVIEWED = "reviewed"


# clinical notes are expected to run 100-3000 words.
# We do not hard-reject notes below 100 words (a short but real note must
# still be submittable) but we do cap the upper end well above the expected
# range as a concrete guard against abuse and runaway LLM cost, rather than
# relying on max_length (character count) alone as a word-count proxy.
MAX_RAW_TEXT_WORDS = 6_000


_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# Matches common US phone formats: 555-123-4567, (555) 123-4567,
# 555.123.4567, 5551234567, optionally with a leading +1/1. Unanchored
# (unlike the 9-digit check) so it also catches a phone number embedded
# within other pseudonym text, matching the email check's behavior.
_PHONE_PATTERN = re.compile(
    r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
)


def _require_identifier(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Identifier cannot be empty or whitespace only")
    return value


def _normalize_pseudonym(value: str | None) -> str | None:
    """Strip optional pseudonyms and reject a few obvious identifier shapes.

    This checks for a small set of unambiguous patterns (bare 9-digit
    numbers, email addresses, phone numbers). It is a narrow guard against
    a clinician accidentally pasting a real identifier here, not
    comprehensive PHI detection -- a determined or careless entry (e.g. a
    patient's actual full name typed as free text) will not be caught.
    """
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    compact = value.replace("-", "").replace(" ", "")
    if compact.isdigit() and len(compact) == 9:
        raise ValueError("pseudonym cannot be a 9-digit identifier")

    if _EMAIL_PATTERN.search(value):
        raise ValueError("pseudonym cannot contain an email address")

    if _PHONE_PATTERN.search(value.strip()):
        raise ValueError("pseudonym cannot be a phone number")

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
        word_count = len(value.split())
        if word_count > MAX_RAW_TEXT_WORDS:
            raise ValueError(
                f"raw_text must not exceed {MAX_RAW_TEXT_WORDS} words "
                f"(got {word_count})"
            )
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
        return _require_identifier(value)

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
        return _require_identifier(value)

    @field_validator("pseudonym")
    @classmethod
    def validate_pseudonym(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None