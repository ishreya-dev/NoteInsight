"""Gemini client behavior with a mocked model boundary."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.models.analysis import DocumentationStatus
from app.services import gemini_client as gemini_module
from app.services.gemini_client import (
    GeminiAnalysisError,
    GeminiClient,
    PROMPT_VERSION,
    build_validation_retry_prompt,
    strip_markdown_fences,
    verify_evidence_quote,
)


def _valid_payload(**overrides: object) -> dict:
    payload: dict = {
        "conditions": [
            {
                "condition_name": "Type 2 diabetes mellitus",
                "evidence_quote": "Type 2 diabetes, controlled on metformin",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 0.92,
            }
        ],
        "gaps": [
            {
                "description": "A1C value not documented",
                "related_condition": "Type 2 diabetes mellitus",
            }
        ],
        "summary": "Follow-up visit for diabetes management.",
    }
    payload.update(overrides)
    return payload


def _make_client(monkeypatch: pytest.MonkeyPatch) -> GeminiClient:
    settings = MagicMock(spec=Settings)
    settings.gemini_api_key = "test-key"
    settings.gemini_model = "gemini-test-model"
    monkeypatch.setattr(
        gemini_module.genai,
        "Client",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        gemini_module,
        "_load_prompt_template",
        lambda: "HEADER\n{note_text}\nFOOTER",
    )
    return GeminiClient(settings)


NOTE = "Patient has Type 2 diabetes, controlled on metformin."


@pytest.mark.asyncio
async def test_empty_note_does_not_call_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock()

    with pytest.raises(GeminiAnalysisError, match="empty"):
        await client.analyze_note("   ")

    client._call_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_response_returns_conditions_gaps_and_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(return_value=json.dumps(_valid_payload()))

    result = await client.analyze_note(NOTE)

    assert result.summary.startswith("Follow-up")
    assert result.model_version == "gemini-test-model"
    assert result.prompt_version == PROMPT_VERSION
    assert len(result.conditions) == 1
    assert result.conditions[0].id
    assert result.conditions[0].quote_verified is True
    assert result.gaps[0].description == "A1C value not documented"


@pytest.mark.asyncio
async def test_hallucinated_quote_marked_unverified_but_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    payload = _valid_payload(
        conditions=[
            {
                "condition_name": "Type 2 diabetes mellitus",
                "evidence_quote": "completely invented quote",
                "documentation_status": "ambiguous",
                "suggested_icd10": "E11.9",
                "confidence": 0.4,
            }
        ]
    )
    client._call_model = AsyncMock(return_value=json.dumps(payload))

    result = await client.analyze_note("Patient reports seasonal allergies.")

    assert result.conditions[0].quote_verified is False
    assert result.conditions[0].evidence_quote == "completely invented quote"


@pytest.mark.asyncio
async def test_condition_ids_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    payload = _valid_payload(
        conditions=[
            {
                "condition_name": "Asthma",
                "evidence_quote": "Asthma is stable",
                "documentation_status": "well_documented",
                "suggested_icd10": "J45.909",
                "confidence": 0.8,
            },
            {
                "condition_name": "Hypertension",
                "evidence_quote": "Hypertension is controlled",
                "documentation_status": "well_documented",
                "suggested_icd10": "I10",
                "confidence": 0.8,
            },
        ]
    )
    client._call_model = AsyncMock(
        return_value=json.dumps(payload)
    )

    result = await client.analyze_note(
        "Asthma is stable. Hypertension is controlled."
    )
    ids = [c.id for c in result.conditions]
    assert len(ids) == len(set(ids)) == 2


@pytest.mark.asyncio
async def test_invalid_then_valid_response_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    invalid = json.dumps({"conditions": [], "gaps": [], "summary": "   "})
    valid = json.dumps(_valid_payload())
    client._call_model = AsyncMock(side_effect=[invalid, valid])

    result = await client.analyze_note(NOTE)

    assert result.summary.startswith("Follow-up")
    assert client._call_model.await_count == 2
    second_prompt = client._call_model.await_args_list[1].args[0]
    assert "CORRECTION REQUIRED" in second_prompt


@pytest.mark.asyncio
async def test_gemini_status_aliases_are_normalized_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    payload = _valid_payload(
        conditions=[
            {
                "condition_name": "Headache",
                "evidence_quote": "intermittent dull headaches for 5 days",
                "documentation_status": "documented",
                "suggested_icd10": "R51.9",
                "confidence": 0.9,
            }
        ],
        gaps=[],
    )
    client._call_model = AsyncMock(return_value=json.dumps(payload))

    result = await client.analyze_note(
        "32-year-old female presents with intermittent dull headaches for 5 days."
    )

    assert result.conditions[0].documentation_status is DocumentationStatus.WELL_DOCUMENTED


@pytest.mark.asyncio
async def test_two_invalid_responses_raise_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(side_effect=["{not-json", "{still-bad"])

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note("Patient presents with cough.")

    assert client._call_model.await_count == 2
    assert exc_info.value.__cause__ is not None
    assert "Patient presents" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_errors_retry_then_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[RuntimeError("timeout"), RuntimeError("timeout")]
    )

    with pytest.raises(GeminiAnalysisError):
        await client.analyze_note("Patient presents with cough.")

    assert client._call_model.await_count == 2


@pytest.mark.asyncio
async def test_programming_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(side_effect=AttributeError("missing text"))

    with pytest.raises(GeminiAnalysisError, match="internal error"):
        await client.analyze_note("Patient presents with cough.")

    assert client._call_model.await_count == 1


@pytest.mark.asyncio
async def test_markdown_fenced_json_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    fenced = (
        "```json\n"
        + json.dumps(_valid_payload())
        + "\n```"
    )
    client._call_model = AsyncMock(return_value=fenced)

    result = await client.analyze_note(NOTE)
    assert result.conditions[0].quote_verified is True


@pytest.mark.asyncio
async def test_prompt_keeps_braces_from_note_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    note = "Dose {special} noted. Type 2 diabetes, controlled on metformin."
    captured: list[str] = []

    async def _capture(prompt: str) -> str:
        captured.append(prompt)
        return json.dumps(_valid_payload())

    client._call_model = _capture
    await client.analyze_note(note)

    assert note in captured[0]
    assert "{special}" in captured[0]


def test_strip_markdown_fences_helpers() -> None:
    fenced = '```json\n{"summary": "ok", "conditions": [], "gaps": []}\n```'
    assert strip_markdown_fences(fenced) == (
        '{"summary": "ok", "conditions": [], "gaps": []}'
    )
    plain = '{"summary": "ok"}'
    assert strip_markdown_fences(plain) == plain


def test_verify_evidence_quote_exact_and_whitespace_tolerant() -> None:
    note = "Patient has Type 2 diabetes."
    assert verify_evidence_quote("Type 2 diabetes", note) is True
    assert verify_evidence_quote("  Type 2 diabetes  ", note) is True
    assert verify_evidence_quote("invented", note) is False


def test_retry_prompt_includes_safe_error_summary() -> None:
    prompt = build_validation_retry_prompt(
        "ORIGINAL PROMPT",
        ValueError("Gemini returned an empty response"),
    )
    assert "ORIGINAL PROMPT" in prompt
    assert "CORRECTION REQUIRED" in prompt
    assert "ValueError" in prompt
