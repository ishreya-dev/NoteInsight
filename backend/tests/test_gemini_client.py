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


class GeminiQuotaError(RuntimeError):
    code = 429


@pytest.mark.asyncio
async def test_empty_note_does_not_call_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock()

    with pytest.raises(GeminiAnalysisError, match="empty") as exc_info:
        await client.analyze_note("   ")

    assert exc_info.value.failure_reason == "empty_note"
    client._call_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_string_note_is_classified_as_empty_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock()

    with pytest.raises(GeminiAnalysisError, match="empty") as exc_info:
        await client.analyze_note("")

    assert exc_info.value.failure_reason == "empty_note"
    client._call_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_empty_note_does_not_call_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)

    async def _stream(prompt: str, attempt: int):
        yield ""

    client._stream_model = _stream

    with pytest.raises(GeminiAnalysisError, match="empty") as exc_info:
        async for _ in client.stream_analyze_note("   "):
            pass

    assert exc_info.value.failure_reason == "empty_note"


@pytest.mark.asyncio
async def test_valid_response_returns_conditions_gaps_and_versions(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
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
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "gemini_attempt_completed" in message
        and "attempt=1" in message
        and "duration_ms=" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_gemini_api_and_response_processing_timing_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    response = MagicMock()
    response.parsed = None
    response.text = json.dumps(_valid_payload())
    client._client.aio.models.generate_content = AsyncMock(return_value=response)

    await client.analyze_note(NOTE)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "gemini_api_completed" in message
        and "attempt=1" in message
        and "duration_ms=" in message
        for message in messages
    )
    assert any(
        "gemini_response_processing_completed" in message
        and "attempt=1" in message
        and "duration_ms=" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_successful_response_emits_safe_structural_metrics(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    response = MagicMock()
    response.parsed = None
    response.text = json.dumps(_valid_payload())
    response.usage_metadata = MagicMock(
        prompt_token_count=100,
        candidates_token_count=42,
        total_token_count=142,
    )
    client._client.aio.models.generate_content = AsyncMock(return_value=response)

    await client.analyze_note(NOTE)

    metrics = next(
        record.getMessage()
        for record in caplog.records
        if "gemini_response_metrics" in record.getMessage()
    )
    assert "attempt=1" in metrics
    assert "condition_count=1" in metrics
    assert "gap_count=1" in metrics
    payload = _valid_payload()
    assert f"summary_char_count={len(payload['summary'])}" in metrics
    assert (
        "max_evidence_quote_char_count="
        f"{len(payload['conditions'][0]['evidence_quote'])}"
    ) in metrics
    assert f"total_response_char_count={len(response.text)}" in metrics
    assert "input_tokens=100" in metrics
    assert "output_tokens=42" in metrics
    assert "total_tokens=142" in metrics
    assert "Follow-up visit" not in metrics
    assert "Type 2 diabetes" not in metrics


@pytest.mark.asyncio
async def test_missing_token_metadata_does_not_break_metrics(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    response = MagicMock()
    response.parsed = None
    response.text = json.dumps(_valid_payload())
    response.usage_metadata = None
    client._client.aio.models.generate_content = AsyncMock(return_value=response)

    await client.analyze_note(NOTE)

    metrics = next(
        record.getMessage()
        for record in caplog.records
        if "gemini_response_metrics" in record.getMessage()
    )
    assert "condition_count=1" in metrics
    assert "input_tokens=" not in metrics
    assert "output_tokens=" not in metrics
    assert "total_tokens=" not in metrics


@pytest.mark.asyncio
async def test_failed_response_does_not_emit_success_metrics(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=RuntimeError("provider failure")
    )

    with pytest.raises(GeminiAnalysisError):
        await client.analyze_note(NOTE)

    assert not any(
        "gemini_response_metrics" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_retry_emits_metrics_only_for_successful_attempt(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[
            json.dumps({"conditions": [], "gaps": [], "summary": "   "}),
            json.dumps(_valid_payload()),
        ]
    )

    await client.analyze_note(NOTE)

    metrics = [
        record.getMessage()
        for record in caplog.records
        if "gemini_response_metrics" in record.getMessage()
    ]
    assert len(metrics) == 1
    assert "attempt=2" in metrics[0]


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
        ],
        gaps=[
            {
                "description": "A1C value not documented",
                "related_condition": "Asthma",
            }
        ],
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
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_module.asyncio, "sleep", sleep)

    with pytest.raises(GeminiAnalysisError):
        await client.analyze_note("Patient presents with cough.")

    assert client._call_model.await_count == 2
    sleep.assert_awaited_once_with(gemini_module._429_FALLBACK_DELAY_SECONDS)


@pytest.mark.asyncio
async def test_429_retry_uses_provider_delay(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    caplog.set_level("INFO")
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[GeminiQuotaError("429 RESOURCE_EXHAUSTED. Please retry in 15.44s"), json.dumps(_valid_payload())]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_module.asyncio, "sleep", sleep)

    result = await client.analyze_note(NOTE)

    assert result.summary.startswith("Follow-up")
    sleep.assert_awaited_once_with(15.44)
    retry_log = next(
        record.getMessage()
        for record in caplog.records
        if "gemini_retry_scheduled" in record.getMessage()
    )
    assert "attempt=1" in retry_log
    assert "error_type=429" in retry_log
    assert "retry_delay_ms=15440" in retry_log
    assert "delay_source=provider" in retry_log


@pytest.mark.asyncio
async def test_429_retry_uses_fallback_delay_when_provider_delay_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[GeminiQuotaError("429 RESOURCE_EXHAUSTED"), json.dumps(_valid_payload())]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_module.asyncio, "sleep", sleep)

    await client.analyze_note(NOTE)

    sleep.assert_awaited_once_with(gemini_module._429_FALLBACK_DELAY_SECONDS)


@pytest.mark.asyncio
async def test_429_retry_delay_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[GeminiQuotaError("429 RESOURCE_EXHAUSTED. Please retry in 999s"), json.dumps(_valid_payload())]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_module.asyncio, "sleep", sleep)

    await client.analyze_note(NOTE)

    sleep.assert_awaited_once_with(gemini_module._429_MAX_DELAY_SECONDS)


@pytest.mark.asyncio
async def test_final_429_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[
            GeminiQuotaError("429 RESOURCE_EXHAUSTED. Please retry in 15.44s"),
            GeminiQuotaError("429 RESOURCE_EXHAUSTED. Please retry in 15.44s"),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_module.asyncio, "sleep", sleep)

    with pytest.raises(GeminiAnalysisError):
        await client.analyze_note(NOTE)

    sleep.assert_awaited_once_with(15.44)


@pytest.mark.asyncio
async def test_final_429_is_classified_as_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[GeminiQuotaError("429 RESOURCE_EXHAUSTED"), GeminiQuotaError("429 RESOURCE_EXHAUSTED")]
    )

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note(NOTE)

    assert exc_info.value.failure_reason == "rate_limited"


@pytest.mark.asyncio
async def test_invalid_output_is_classified_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(side_effect=["{bad", "{still bad"])

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note(NOTE)

    assert exc_info.value.failure_reason == "invalid_output"


@pytest.mark.asyncio
async def test_timeout_is_classified_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[TimeoutError("provider timeout"), TimeoutError("provider timeout")]
    )

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note(NOTE)

    assert exc_info.value.failure_reason == "timeout"


@pytest.mark.asyncio
async def test_provider_error_is_classified_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(
        side_effect=[RuntimeError("provider unavailable"), RuntimeError("provider unavailable")]
    )

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note(NOTE)

    assert exc_info.value.failure_reason == "provider_error"


@pytest.mark.asyncio
async def test_unexpected_internal_error_is_classified_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._call_model = AsyncMock(side_effect=AttributeError("missing response"))

    with pytest.raises(GeminiAnalysisError) as exc_info:
        await client.analyze_note(NOTE)

    assert exc_info.value.failure_reason == "internal_error"


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

    async def _capture(prompt: str, attempt: int) -> str:
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


def test_verify_evidence_quote_rejects_whitespace_only_quote() -> None:
    note = "Patient has Type 2 diabetes."
    assert verify_evidence_quote("   ", note) is False


def test_verify_evidence_quote_rejects_single_character_quote() -> None:
    note = "Patient has Type 2 diabetes."
    assert verify_evidence_quote("a", note) is False
    assert verify_evidence_quote("B", note) is False


def test_verify_evidence_quote_rejects_trivial_short_quote() -> None:
    note = "Patient has Type 2 diabetes."
    assert verify_evidence_quote("ab", note) is False
    assert verify_evidence_quote("xy", note) is False


def test_verify_evidence_quote_accepts_short_clinically_meaningful_quote() -> None:
    note = "A1C was 7.0%."
    assert verify_evidence_quote("A1C", note) is True


def test_verify_evidence_quote_nfc_normalized_matching_still_works() -> None:
    note = "Patient has café."
    assert verify_evidence_quote("cafe\u0301", note) is True


def test_retry_prompt_includes_safe_error_summary() -> None:
    prompt = build_validation_retry_prompt(
        "ORIGINAL PROMPT",
        ValueError("Gemini returned an empty response"),
    )
    assert "ORIGINAL PROMPT" in prompt
    assert "CORRECTION REQUIRED" in prompt
    assert "ValueError" in prompt
