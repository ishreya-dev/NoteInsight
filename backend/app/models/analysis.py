"""Models for AI-generated analysis and clinician review data."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.base import utcnow


MAX_CONDITIONS_PER_ANALYSIS = 50
MAX_GAPS_PER_ANALYSIS = 50
MAX_CONDITIONS_PER_REVIEW = 50
MAX_GAPS_PER_REVIEW = 50


class DocumentationStatus(str, Enum):
    WELL_DOCUMENTED = "well_documented"
    AMBIGUOUS = "ambiguous"
    MENTIONED_WITHOUT_ASSESSMENT = "mentioned_without_assessment_or_plan"


class ConditionReviewStatus(str, Enum):
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    ADDED = "added"


class AnalysisJobStatus(str, Enum):
    """Lifecycle status of a background analysis job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def _require_non_whitespace(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Value must not be empty or whitespace only")
    return value


def _normalize_optional_text(value: str | None) -> str | None:
    """Strip optional text; treat blank as absent."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_evidence_quote(value: str) -> str:
    # Do not strip the returned value: quotes must remain verbatim for
    # traceability checks against the source note.
    if not value.strip():
        raise ValueError("evidence_quote must not be empty or whitespace only")
    return value


def _reject_duplicate_source_ids(source_ids: list[str]) -> None:
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("A source condition cannot be reviewed more than once")


def _normalize_condition_name(value: str) -> str:
    normalized = value.strip().lower()
    normalized = " ".join(normalized.split())
    return normalized


class ConditionFromModel(BaseModel):
    """Mutable condition shape returned by the model before persistence."""

    condition_name: str = Field(min_length=1, max_length=200)
    evidence_quote: str = Field(min_length=1, max_length=1000)
    documentation_status: DocumentationStatus
    suggested_icd10: str = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("condition_name", "suggested_icd10")
    @classmethod
    def validate_non_whitespace(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("evidence_quote")
    @classmethod
    def validate_evidence_quote(cls, value: str) -> str:
        return _require_evidence_quote(value)


class Condition(BaseModel):
    """Immutable condition stored on an Analysis."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    condition_name: str = Field(min_length=1, max_length=200)
    evidence_quote: str = Field(min_length=1, max_length=1000)
    documentation_status: DocumentationStatus
    suggested_icd10: str = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    quote_verified: bool = False

    @field_validator("id", "condition_name", "suggested_icd10")
    @classmethod
    def validate_non_whitespace(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("evidence_quote")
    @classmethod
    def validate_evidence_quote(cls, value: str) -> str:
        return _require_evidence_quote(value)


class DocumentationGapFromModel(BaseModel):
    """Mutable documentation gap from the model or clinician review input."""

    description: str = Field(min_length=1, max_length=500)
    related_condition: str | None = Field(default=None, max_length=200)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("related_condition")
    @classmethod
    def validate_related_condition(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class DocumentationGap(BaseModel):
    """Immutable documentation gap stored on an Analysis."""

    model_config = ConfigDict(frozen=True)

    description: str = Field(min_length=1, max_length=500)
    related_condition: str | None = Field(default=None, max_length=200)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("related_condition")
    @classmethod
    def validate_related_condition(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class GeminiRawResponse(BaseModel):
    """Validated structured output from Gemini before conversion to Analysis."""

    conditions: list[ConditionFromModel] = Field(
        default_factory=list,
        max_length=MAX_CONDITIONS_PER_ANALYSIS,
    )
    gaps: list[DocumentationGapFromModel] = Field(
        default_factory=list,
        max_length=MAX_GAPS_PER_ANALYSIS,
    )
    summary: str = Field(min_length=1, max_length=2000)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @model_validator(mode="after")
    def validate_unique_condition_names(self) -> "GeminiRawResponse":
        seen: set[str] = set()
        for condition in self.conditions:
            normalized = _normalize_condition_name(condition.condition_name)
            if normalized in seen:
                raise ValueError(
                    f"Duplicate condition name: {condition.condition_name!r}"
                )
            seen.add(normalized)
        return self

    @model_validator(mode="after")
    def validate_related_condition_references(self) -> "GeminiRawResponse":
        if not self.gaps:
            return self
        condition_names = {
            _normalize_condition_name(condition.condition_name)
            for condition in self.conditions
        }
        for gap in self.gaps:
            if gap.related_condition is not None:
                normalized = _normalize_condition_name(gap.related_condition)
                if normalized not in condition_names:
                    raise ValueError(
                        f"related_condition {gap.related_condition!r} does not match any condition name"
                    )
        return self


class Analysis(BaseModel):
    """Immutable AI analysis stored in Firestore."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    note_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)

    conditions: tuple[Condition, ...] = Field(
        max_length=MAX_CONDITIONS_PER_ANALYSIS,
    )
    gaps: tuple[DocumentationGap, ...] = Field(
        max_length=MAX_GAPS_PER_ANALYSIS,
    )
    summary: str = Field(min_length=1, max_length=2000)

    model_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)

    created_at: datetime = Field(default_factory=utcnow)

    is_failed: bool = False
    failure_reason: str | None = Field(default=None, max_length=2000)

    @field_validator(
        "id",
        "note_id",
        "user_id",
        "model_version",
        "prompt_version",
        "summary",
    )
    @classmethod
    def validate_non_whitespace(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("failure_reason")
    @classmethod
    def validate_failure_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("failure_reason must not be whitespace only")
        return value

    @model_validator(mode="after")
    def validate_failure_state(self) -> "Analysis":
        if self.is_failed:
            if not self.failure_reason:
                raise ValueError(
                    "failure_reason is required when is_failed is True"
                )
            if self.conditions or self.gaps:
                raise ValueError(
                    "failed analysis must not contain conditions or gaps"
                )
        elif self.failure_reason is not None:
            raise ValueError(
                "failure_reason must be None when is_failed is False"
            )
        return self


class ConditionReview(BaseModel):
    """Clinician-reviewed condition; may accept, edit, reject, or add."""

    source_condition_id: str | None = Field(default=None, min_length=1)
    condition_name: str = Field(min_length=1, max_length=200)
    evidence_quote: str = Field(min_length=1, max_length=1000)
    documentation_status: DocumentationStatus
    suggested_icd10: str = Field(min_length=1, max_length=20)
    status: ConditionReviewStatus

    @field_validator("source_condition_id", "condition_name", "suggested_icd10")
    @classmethod
    def validate_non_whitespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_whitespace(value)

    @field_validator("evidence_quote")
    @classmethod
    def validate_evidence_quote(cls, value: str) -> str:
        return _require_evidence_quote(value)

    @model_validator(mode="after")
    def check_source_id_matches_status(self) -> "ConditionReview":
        is_added = self.status == ConditionReviewStatus.ADDED
        has_source = self.source_condition_id is not None

        if is_added and has_source:
            raise ValueError("status 'added' must not have a source_condition_id")

        if not is_added and not has_source:
            raise ValueError(
                f"status '{self.status.value}' requires a source_condition_id"
            )

        return self


class ReviewCreate(BaseModel):
    """Client payload for creating or updating a review."""

    conditions: list[ConditionReview] = Field(
        default_factory=list,
        max_length=MAX_CONDITIONS_PER_REVIEW,
    )
    gaps: list[DocumentationGapFromModel] = Field(
        default_factory=list,
        max_length=MAX_GAPS_PER_REVIEW,
    )
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def reject_duplicate_source_conditions(self) -> "ReviewCreate":
        source_ids = [
            condition.source_condition_id
            for condition in self.conditions
            if condition.source_condition_id is not None
        ]
        _reject_duplicate_source_ids(source_ids)
        return self


class Review(BaseModel):
    """Clinician review stored in Firestore (separate from Analysis)."""

    id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    note_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)

    conditions: list[ConditionReview] = Field(
        max_length=MAX_CONDITIONS_PER_REVIEW,
    )
    gaps: list[DocumentationGapFromModel] = Field(
        max_length=MAX_GAPS_PER_REVIEW,
    )
    reviewer_notes: str | None = Field(default=None, max_length=1000)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("id", "analysis_id", "note_id", "user_id")
    @classmethod
    def validate_non_whitespace(cls, value: str) -> str:
        return _require_non_whitespace(value)

    @field_validator("reviewer_notes")
    @classmethod
    def validate_reviewer_notes(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def reject_duplicate_source_conditions(self) -> "Review":
        source_ids = [
            condition.source_condition_id
            for condition in self.conditions
            if condition.source_condition_id is not None
        ]
        _reject_duplicate_source_ids(source_ids)
        return self