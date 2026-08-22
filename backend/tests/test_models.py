"""Domain model and configuration validation."""

from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import ValidationError

from app.config import MissingConfigError, Settings
from app.models.analysis import (
    Analysis,
    Condition,
    ConditionFromModel,
    ConditionReview,
    ConditionReviewStatus,
    DocumentationGap,
    DocumentationGapFromModel,
    DocumentationStatus,
    GeminiRawResponse,
    ReviewCreate,
)
from app.models.note import NoteCreate
from app.models.user import AuthenticatedUser


def _valid_condition_from_model(**overrides: object) -> ConditionFromModel:
    payload = {
        "condition_name": "Type 2 diabetes mellitus",
        "evidence_quote": "Type 2 diabetes, controlled",
        "documentation_status": DocumentationStatus.WELL_DOCUMENTED,
        "suggested_icd10": "E11.9",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return ConditionFromModel(**payload)


# ---- ConditionFromModel ----


def test_condition_from_model_accepts_valid_payload() -> None:
    condition = _valid_condition_from_model()
    assert condition.condition_name == "Type 2 diabetes mellitus"
    assert condition.confidence == 0.8


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_condition_from_model_accepts_confidence_boundaries(confidence: float) -> None:
    condition = _valid_condition_from_model(confidence=confidence)
    assert condition.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_condition_from_model_rejects_out_of_range_confidence(
    confidence: float,
) -> None:
    with pytest.raises(ValidationError):
        _valid_condition_from_model(confidence=confidence)


@pytest.mark.parametrize("condition_name", ["", "   "])
def test_condition_from_model_rejects_blank_condition_name(
    condition_name: str,
) -> None:
    with pytest.raises(ValidationError):
        _valid_condition_from_model(condition_name=condition_name)


@pytest.mark.parametrize("evidence_quote", ["", "   "])
def test_condition_from_model_rejects_blank_evidence_quote(
    evidence_quote: str,
) -> None:
    with pytest.raises(ValidationError):
        _valid_condition_from_model(evidence_quote=evidence_quote)


def test_condition_from_model_rejects_invalid_documentation_status() -> None:
    with pytest.raises(ValidationError):
        _valid_condition_from_model(documentation_status="not_a_status")


@pytest.mark.parametrize("suggested_icd10", ["", "   "])
def test_condition_from_model_rejects_blank_icd10(suggested_icd10: str) -> None:
    with pytest.raises(ValidationError):
        _valid_condition_from_model(suggested_icd10=suggested_icd10)


# ---- Condition (stored / immutable) ----


def test_condition_defaults_quote_verified_to_false() -> None:
    condition = Condition(
        id="c1",
        condition_name="Asthma",
        evidence_quote="asthma is stable",
        documentation_status=DocumentationStatus.AMBIGUOUS,
        suggested_icd10="J45.909",
        confidence=0.5,
    )
    assert condition.quote_verified is False


def test_condition_is_immutable() -> None:
    condition = Condition(
        id="c1",
        condition_name="Asthma",
        evidence_quote="asthma is stable",
        documentation_status=DocumentationStatus.AMBIGUOUS,
        suggested_icd10="J45.909",
        confidence=0.5,
        quote_verified=True,
    )
    with pytest.raises(ValidationError):
        condition.condition_name = "changed"


# ---- Documentation gaps ----


def test_documentation_gap_accepts_null_related_condition() -> None:
    gap = DocumentationGapFromModel(
        description="Severity not documented",
        related_condition=None,
    )
    assert gap.related_condition is None


def test_documentation_gap_blank_related_condition_becomes_none() -> None:
    gap = DocumentationGapFromModel(
        description="Severity not documented",
        related_condition="   ",
    )
    assert gap.related_condition is None


@pytest.mark.parametrize("description", ["", "   "])
def test_documentation_gap_rejects_blank_description(description: str) -> None:
    with pytest.raises(ValidationError):
        DocumentationGapFromModel(description=description)


def test_stored_documentation_gap_is_immutable() -> None:
    gap = DocumentationGap(description="Missing A1C", related_condition=None)
    with pytest.raises(ValidationError):
        gap.description = "changed"


# ---- Reviews ----


def test_condition_review_added_cannot_have_source_id() -> None:
    with pytest.raises(ValidationError):
        ConditionReview(
            source_condition_id="c1",
            condition_name="Asthma",
            evidence_quote="wheezing",
            documentation_status=DocumentationStatus.AMBIGUOUS,
            suggested_icd10="J45.909",
            status=ConditionReviewStatus.ADDED,
        )


def test_condition_review_accepted_requires_source_id() -> None:
    with pytest.raises(ValidationError):
        ConditionReview(
            source_condition_id=None,
            condition_name="Asthma",
            evidence_quote="wheezing",
            documentation_status=DocumentationStatus.AMBIGUOUS,
            suggested_icd10="J45.909",
            status=ConditionReviewStatus.ACCEPTED,
        )


def test_review_create_rejects_duplicate_source_ids() -> None:
    shared = {
        "condition_name": "Diabetes",
        "evidence_quote": "Type 2 diabetes",
        "documentation_status": DocumentationStatus.AMBIGUOUS,
        "suggested_icd10": "E11.9",
    }
    with pytest.raises(ValidationError):
        ReviewCreate(
            conditions=[
                ConditionReview(
                    source_condition_id="c1",
                    status=ConditionReviewStatus.ACCEPTED,
                    **shared,
                ),
                ConditionReview(
                    source_condition_id="c1",
                    status=ConditionReviewStatus.EDITED,
                    **shared,
                ),
            ]
        )


# ---- Analysis ----


def test_failed_analysis_cannot_include_conditions() -> None:
    condition = Condition(
        id="c1",
        condition_name="Diabetes",
        evidence_quote="Type 2 diabetes",
        documentation_status=DocumentationStatus.AMBIGUOUS,
        suggested_icd10="E11.9",
        confidence=0.5,
    )
    with pytest.raises(ValidationError):
        Analysis(
            id="a1",
            note_id="n1",
            user_id="u1",
            conditions=(condition,),
            gaps=(),
            summary="failed",
            model_version="m",
            prompt_version="v1",
            is_failed=True,
            failure_reason="boom",
        )


def test_failed_analysis_requires_failure_reason() -> None:
    with pytest.raises(ValidationError):
        Analysis(
            id="a1",
            note_id="n1",
            user_id="u1",
            conditions=(),
            gaps=(),
            summary="failed",
            model_version="m",
            prompt_version="v1",
            is_failed=True,
            failure_reason=None,
        )


def test_gemini_raw_response_accepts_unique_conditions() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
            ConditionFromModel(
                condition_name="Hypertension",
                evidence_quote="blood pressure controlled",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="I10",
                confidence=0.8,
            ),
        ],
        gaps=[],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_rejects_exact_duplicate_conditions() -> None:
    with pytest.raises(ValidationError):
        GeminiRawResponse(
            conditions=[
                ConditionFromModel(
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote="controlled on metformin",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
                ConditionFromModel(
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote="different quote",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
            ],
            gaps=[],
            summary="Follow-up visit.",
        )


def test_gemini_raw_response_rejects_case_only_duplicate_conditions() -> None:
    with pytest.raises(ValidationError):
        GeminiRawResponse(
            conditions=[
                ConditionFromModel(
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote="controlled on metformin",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
                ConditionFromModel(
                    condition_name="type 2 diabetes mellitus",
                    evidence_quote="different quote",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
            ],
            gaps=[],
            summary="Follow-up visit.",
        )


def test_gemini_raw_response_rejects_whitespace_only_duplicate_conditions() -> None:
    with pytest.raises(ValidationError):
        GeminiRawResponse(
            conditions=[
                ConditionFromModel(
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote="controlled on metformin",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
                ConditionFromModel(
                    condition_name="  type 2  diabetes  mellitus  ",
                    evidence_quote="different quote",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
            ],
            gaps=[],
            summary="Follow-up visit.",
        )


def test_gemini_raw_response_accepts_different_condition_names() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
            ConditionFromModel(
                condition_name="Type 1 diabetes mellitus",
                evidence_quote="on insulin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E10.9",
                confidence=0.9,
            ),
        ],
        gaps=[],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_accepts_matching_related_condition() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
        ],
        gaps=[
            DocumentationGapFromModel(
                description="A1C value not documented",
                related_condition="Type 2 diabetes mellitus",
            ),
        ],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_accepts_case_only_difference_related_condition() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
        ],
        gaps=[
            DocumentationGapFromModel(
                description="A1C value not documented",
                related_condition="type 2 diabetes mellitus",
            ),
        ],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_accepts_whitespace_only_difference_related_condition() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
        ],
        gaps=[
            DocumentationGapFromModel(
                description="A1C value not documented",
                related_condition="  type 2  diabetes  mellitus  ",
            ),
        ],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_rejects_non_existent_related_condition() -> None:
    with pytest.raises(ValidationError):
        GeminiRawResponse(
            conditions=[
                ConditionFromModel(
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote="controlled on metformin",
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                ),
            ],
            gaps=[
                DocumentationGapFromModel(
                    description="A1C value not documented",
                    related_condition="Hypertension",
                ),
            ],
            summary="Follow-up visit.",
        )


def test_gemini_raw_response_accepts_none_related_condition() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
        ],
        gaps=[
            DocumentationGapFromModel(
                description="A1C value not documented",
                related_condition=None,
            ),
        ],
        summary="Follow-up visit.",
    )


def test_gemini_raw_response_accepts_missing_related_condition() -> None:
    GeminiRawResponse(
        conditions=[
            ConditionFromModel(
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="controlled on metformin",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.9,
            ),
        ],
        gaps=[
            DocumentationGapFromModel(
                description="A1C value not documented",
            ),
        ],
        summary="Follow-up visit.",
    )


# ---- Notes ----


def test_note_create_accepts_valid_note_and_optional_fields() -> None:
    note = NoteCreate(
        raw_text="Patient presents with cough.",
        pseudonym="Pt-A",
        visit_date=date(2024, 6, 1),
    )
    assert note.pseudonym == "Pt-A"
    assert note.visit_date == date(2024, 6, 1)


@pytest.mark.parametrize("raw_text", ["", "   \n\t  "])
def test_note_create_rejects_blank_text(raw_text: str) -> None:
    with pytest.raises(ValidationError):
        NoteCreate(raw_text=raw_text)


def test_note_create_rejects_text_over_max_length() -> None:
    with pytest.raises(ValidationError):
        NoteCreate(raw_text="x" * 20_001)


def test_note_create_normalizes_blank_pseudonym_to_none() -> None:
    note = NoteCreate(raw_text="Patient presents with cough.", pseudonym="  ")
    assert note.pseudonym is None


def test_note_create_rejects_nine_digit_pseudonym() -> None:
    with pytest.raises(ValidationError):
        NoteCreate(
            raw_text="Patient presents with cough.",
            pseudonym="123-45-6789",
        )


# ---- Users / config ----


def test_authenticated_user_accepts_non_strict_email() -> None:
    user = AuthenticatedUser(uid="uid-1", email="not-a-strict-email")
    assert user.email == "not-a-strict-email"


def test_authenticated_user_rejects_blank_uid() -> None:
    with pytest.raises(ValidationError):
        AuthenticatedUser(uid="   ")


def test_settings_rejects_blank_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo")
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_PATH", raising=False)
    with pytest.raises(MissingConfigError):
        Settings()


def test_settings_falls_back_when_optional_model_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo")
    monkeypatch.setenv("GEMINI_MODEL", "  ")
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_PATH", raising=False)
    settings = Settings()
    assert settings.gemini_model == "gemini-3.6-flash"


def test_settings_rejects_missing_service_account_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo")
    monkeypatch.setenv(
        "FIREBASE_SERVICE_ACCOUNT_PATH",
        os.path.join("does", "not", "exist.json"),
    )
    with pytest.raises(MissingConfigError):
        Settings()
