"""Gemini client for structured clinical note analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models.analysis import (
    Condition,
    ConditionFromModel,
    DocumentationGap,
    DocumentationGapFromModel,
    GeminiRawResponse,
)

logger = logging.getLogger(__name__)

_response_processing_started_at: ContextVar[float | None] = ContextVar(
    "response_processing_started_at",
    default=None,
)
_response_usage_metadata: ContextVar[dict[str, int] | None] = ContextVar(
    "response_usage_metadata",
    default=None,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "analysis_prompt.txt"
)
_NOTE_PLACEHOLDER = "{note_text}"
PROMPT_VERSION = "v1"
_MAX_ATTEMPTS = 2
_429_FALLBACK_DELAY_SECONDS = 1.0
_429_MAX_DELAY_SECONDS = 30.0
_DOCUMENTATION_STATUS_ALIASES = {
    "documented": "well_documented",
    "well-documented": "well_documented",
    "well_documented": "well_documented",
    "ambiguous": "ambiguous",
    "unclear": "ambiguous",
    "suspected": "ambiguous",
    "mentioned_without_assessment_or_plan": "mentioned_without_assessment_or_plan",
    "mentioned_without_assessment": "mentioned_without_assessment_or_plan",
    "mentioned_no_assessment_or_plan": "mentioned_without_assessment_or_plan",
    "no_assessment_or_plan": "mentioned_without_assessment_or_plan",
}
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "conditions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "condition_name": {"type": "STRING"},
                    "evidence_quote": {"type": "STRING"},
                    "documentation_status": {
                        "type": "STRING",
                        "enum": [
                            "well_documented",
                            "ambiguous",
                            "mentioned_without_assessment_or_plan",
                        ],
                    },
                    "suggested_icd10": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": [
                    "condition_name",
                    "evidence_quote",
                    "documentation_status",
                    "suggested_icd10",
                    "confidence",
                ],
            },
        },
        "gaps": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "related_condition": {"type": "STRING", "nullable": True},
                },
                "required": ["description"],
            },
        },
        "summary": {"type": "STRING"},
    },
    "required": ["conditions", "gaps", "summary"],
}

_FENCE_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)


class GeminiAnalysisError(RuntimeError):
    """Raised when Gemini fails to produce valid structured output."""

    def __init__(self, message: str, *, failure_reason: str = "unknown") -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


def _failure_reason_for_exception(exc: Exception) -> str:
    if _is_resource_exhausted(exc):
        return "rate_limited"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    return "provider_error"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Validated, quote-checked analysis ready for persistence."""

    conditions: list[Condition]
    gaps: list[DocumentationGap]
    summary: str
    model_version: str
    prompt_version: str


def strip_markdown_fences(text: str) -> str:
    """Remove a surrounding markdown code fence if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    match = _FENCE_PATTERN.match(stripped)
    if match is not None:
        return match.group("body").strip()

    lines = stripped.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def build_validation_retry_prompt(
    original_prompt: str,
    validation_error: Exception,
) -> str:
    """Ask the model for corrected JSON using a truncated schema error."""
    reason = _safe_error_summary(validation_error)
    if len(reason) > 800:
        reason = reason[:800] + "..."

    return (
        f"{original_prompt}\n\n"
        "CORRECTION REQUIRED\n"
        "Your previous response did not match the required JSON schema.\n"
        f"Validation error: {reason}\n"
        "Return only a single corrected JSON object that satisfies the schema. "
        "Do not include markdown, code fences, or any other text."
    )


def verify_evidence_quote(evidence_quote: str, note_text: str) -> bool:
    """Return True when the quote appears in the note without altering either string.

    Uses exact containment first, then NFC-normalized stripped containment so
    trivial whitespace/Unicode differences do not falsely mark a quote unverified.
    """
    if evidence_quote in note_text:
        return True

    normalized_quote = unicodedata.normalize("NFC", evidence_quote.strip())
    if not normalized_quote:
        return False

    normalized_note = unicodedata.normalize("NFC", note_text)
    return normalized_quote in normalized_note


def _safe_error_summary(exc: Exception) -> str:
    """Summarize an error without embedding model/note field values."""
    if isinstance(exc, ValidationError):
        parts: list[str] = []
        for err in exc.errors()[:8]:
            loc = ".".join(str(item) for item in err.get("loc", ()))
            err_type = err.get("type", "error")
            parts.append(f"{loc}:{err_type}" if loc else err_type)
        detail = "; ".join(parts) if parts else "invalid"
        return f"ValidationError({detail})"

    if isinstance(exc, ValueError):
        message = str(exc).strip()
        if message:
            return f"ValueError: {message[:200]}"
        return "ValueError"

    return type(exc).__name__


def _status_code_from_exception(exc: Exception) -> int | str | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_resource_exhausted(exc: Exception) -> bool:
    status_code = _status_code_from_exception(exc)
    if status_code == 429 or str(status_code).upper() == "RESOURCE_EXHAUSTED":
        return True
    message = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in message or "429" in message


def _retry_delay_from_exception(exc: Exception) -> tuple[float, str]:
    match = re.search(r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s", str(exc), re.IGNORECASE)
    if match is None:
        return _429_FALLBACK_DELAY_SECONDS, "fallback"

    requested_delay = float(match.group(1))
    return min(requested_delay, _429_MAX_DELAY_SECONDS), "provider"


def _normalize_documentation_status(value: object) -> object:
    if value is None:
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return value
    return _DOCUMENTATION_STATUS_ALIASES.get(normalized, normalized)


def _normalize_response_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload

    conditions = payload.get("conditions")
    if isinstance(conditions, list):
        normalized_conditions: list[dict[str, object]] = []
        for condition in conditions:
            if not isinstance(condition, dict):
                normalized_conditions.append(condition)  # type: ignore[arg-type]
                continue
            normalized_condition = dict(condition)
            status_value = normalized_condition.get("documentation_status")
            normalized_condition["documentation_status"] = (
                _normalize_documentation_status(status_value)
            )
            normalized_conditions.append(normalized_condition)
        payload["conditions"] = normalized_conditions

    return payload


def _load_prompt_template() -> str:
    try:
        template = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Unable to load Gemini prompt from '{_PROMPT_PATH}'"
        ) from exc

    if _NOTE_PLACEHOLDER not in template:
        raise RuntimeError(
            "Gemini prompt template is missing the "
            f"{_NOTE_PLACEHOLDER} placeholder"
        )
    return template


class GeminiClient:
    """Calls Gemini and returns validated structured analysis results."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._prompt_template = _load_prompt_template()

    async def analyze_note(self, note_text: str) -> AnalysisResult:
        """Analyze a clinical note and return validated structured output."""
        if not note_text.strip():
            raise GeminiAnalysisError("Clinical note text is empty")

        base_prompt = self._prompt_template.replace(
            _NOTE_PLACEHOLDER,
            note_text,
        )
        request_contents = base_prompt
        last_error: Exception | None = None
        last_failure_reason = "invalid_output"

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempt_started_at = time.perf_counter()
            processing_token = _response_processing_started_at.set(None)
            usage_token = _response_usage_metadata.set(None)
            try:
                raw_text = await self._call_model(request_contents, attempt)
                processing_started_at = _response_processing_started_at.get()
                if processing_started_at is None:
                    processing_started_at = time.perf_counter()
                    _response_processing_started_at.set(processing_started_at)
                cleaned = strip_markdown_fences(raw_text)
                if not cleaned:
                    raise ValueError("Gemini returned an empty response")

                try:
                    parsed_obj = json.loads(cleaned)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "Gemini returned malformed JSON"
                    ) from exc

                normalized = _normalize_response_payload(parsed_obj)
                parsed = GeminiRawResponse.model_validate(normalized)
                result = self._to_result(parsed, note_text)
                self._log_response_metrics(
                    attempt=attempt,
                    result=result,
                    response_text=raw_text,
                    usage_metadata=_response_usage_metadata.get(),
                )
                return result

            except (ValidationError, ValueError) as exc:
                last_error = exc
                last_failure_reason = "invalid_output"
                logger.warning(
                    "Gemini output validation failed on attempt %d/%d model=%s "
                    "error=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                    _safe_error_summary(exc),
                )
                if attempt < _MAX_ATTEMPTS:
                    request_contents = build_validation_retry_prompt(
                        base_prompt,
                        exc,
                    )

            except (TypeError, AttributeError) as exc:
                logger.exception(
                    "Unexpected Gemini client error on attempt %d/%d model=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                )
                raise GeminiAnalysisError(
                    "Gemini analysis failed due to an internal error",
                    failure_reason="internal_error",
                ) from exc

            except Exception as exc:
                last_error = exc
                last_failure_reason = _failure_reason_for_exception(exc)
                logger.warning(
                    "Gemini API request failed on attempt %d/%d model=%s "
                    "error_type=%s error_message=%s status_code=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                    type(exc).__name__,
                    str(exc)[:500],
                    _status_code_from_exception(exc),
                )
                if attempt < _MAX_ATTEMPTS:
                    request_contents = base_prompt
                    if _is_resource_exhausted(exc):
                        retry_delay, delay_source = _retry_delay_from_exception(exc)
                        logger.info(
                            "gemini_retry_scheduled attempt=%d error_type=429 "
                            "retry_delay_ms=%d delay_source=%s",
                            attempt,
                            round(retry_delay * 1000),
                            delay_source,
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.info(
                            "gemini_retry_scheduled attempt=%d error_type=%s "
                            "retry_delay_ms=%d delay_source=fallback",
                            attempt,
                            type(exc).__name__,
                            round(_429_FALLBACK_DELAY_SECONDS * 1000),
                        )
                        await asyncio.sleep(_429_FALLBACK_DELAY_SECONDS)
            finally:
                processing_started_at = _response_processing_started_at.get()
                if processing_started_at is not None:
                    logger.info(
                        "gemini_response_processing_completed attempt=%d "
                        "duration_ms=%d",
                        attempt,
                        round((time.perf_counter() - processing_started_at) * 1000),
                    )
                _response_processing_started_at.reset(processing_token)
                _response_usage_metadata.reset(usage_token)
                logger.info(
                    "gemini_attempt_completed attempt=%d duration_ms=%d",
                    attempt,
                    round((time.perf_counter() - attempt_started_at) * 1000),
                )

        raise GeminiAnalysisError(
            f"Gemini did not return valid output after {_MAX_ATTEMPTS} attempts",
            failure_reason=last_failure_reason,
        ) from last_error

    async def stream_analyze_note(
        self, note_text: str
    ) -> AsyncIterator[str]:
        """Yield raw text chunks from Gemini for incremental streaming.

        Accumulated text is not validated here; the caller is responsible
        for final validation and persistence.
        """
        if not note_text.strip():
            raise GeminiAnalysisError("Clinical note text is empty")

        base_prompt = self._prompt_template.replace(
            _NOTE_PLACEHOLDER,
            note_text,
        )
        request_contents = base_prompt
        last_error: Exception | None = None
        last_failure_reason = "invalid_output"

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempt_started_at = time.perf_counter()
            processing_token = _response_processing_started_at.set(None)
            usage_token = _response_usage_metadata.set(None)
            try:
                async for chunk in self._stream_model(request_contents, attempt):
                    if chunk:
                        yield chunk
                return

            except (ValidationError, ValueError) as exc:
                last_error = exc
                last_failure_reason = "invalid_output"
                logger.warning(
                    "Gemini output validation failed on attempt %d/%d model=%s "
                    "error=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                    _safe_error_summary(exc),
                )
                if attempt < _MAX_ATTEMPTS:
                    request_contents = build_validation_retry_prompt(
                        base_prompt,
                        exc,
                    )

            except (TypeError, AttributeError) as exc:
                logger.exception(
                    "Unexpected Gemini client error on attempt %d/%d model=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                )
                raise GeminiAnalysisError(
                    "Gemini analysis failed due to an internal error",
                    failure_reason="internal_error",
                ) from exc

            except Exception as exc:
                last_error = exc
                last_failure_reason = _failure_reason_for_exception(exc)
                logger.warning(
                    "Gemini API request failed on attempt %d/%d model=%s "
                    "error_type=%s error_message=%s status_code=%s",
                    attempt,
                    _MAX_ATTEMPTS,
                    self._settings.gemini_model,
                    type(exc).__name__,
                    str(exc)[:500],
                    _status_code_from_exception(exc),
                )
                if attempt < _MAX_ATTEMPTS:
                    request_contents = base_prompt
                    if _is_resource_exhausted(exc):
                        retry_delay, delay_source = _retry_delay_from_exception(exc)
                        logger.info(
                            "gemini_retry_scheduled attempt=%d error_type=429 "
                            "retry_delay_ms=%d delay_source=%s",
                            attempt,
                            round(retry_delay * 1000),
                            delay_source,
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.info(
                            "gemini_retry_scheduled attempt=%d error_type=%s "
                            "retry_delay_ms=%d delay_source=fallback",
                            attempt,
                            type(exc).__name__,
                            round(_429_FALLBACK_DELAY_SECONDS * 1000),
                        )
                        await asyncio.sleep(_429_FALLBACK_DELAY_SECONDS)
            finally:
                processing_started_at = _response_processing_started_at.get()
                if processing_started_at is not None:
                    logger.info(
                        "gemini_response_processing_completed attempt=%d "
                        "duration_ms=%d",
                        attempt,
                        round((time.perf_counter() - processing_started_at) * 1000),
                    )
                _response_processing_started_at.reset(processing_token)
                _response_usage_metadata.reset(usage_token)
                logger.info(
                    "gemini_attempt_completed attempt=%d duration_ms=%d",
                    attempt,
                    round((time.perf_counter() - attempt_started_at) * 1000),
                )

        raise GeminiAnalysisError(
            f"Gemini did not return valid output after {_MAX_ATTEMPTS} attempts",
            failure_reason=last_failure_reason,
        ) from last_error

    @staticmethod
    def _log_response_metrics(
        *,
        attempt: int,
        result: AnalysisResult,
        response_text: str,
        usage_metadata: dict[str, int] | None,
    ) -> None:
        fields = [
            f"attempt={attempt}",
            f"condition_count={len(result.conditions)}",
            f"gap_count={len(result.gaps)}",
            f"summary_char_count={len(result.summary)}",
            "max_evidence_quote_char_count="
            f"{max((len(condition.evidence_quote) for condition in result.conditions), default=0)}",
            f"total_response_char_count={len(response_text)}",
        ]
        if usage_metadata is not None:
            fields.extend(
                f"{name}={usage_metadata[name]}"
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
                if name in usage_metadata
            )
        logger.info("gemini_response_metrics %s", " ".join(fields))

    async def _stream_model(self, prompt: str, attempt: int) -> AsyncIterator[str]:
        """Call Gemini streaming and yield raw text chunks."""
        api_started_at = time.perf_counter()
        stream = None
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._settings.gemini_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=0.2,
                ),
            )
            usage_metadata_captured = False
            async for response in stream:
                if not usage_metadata_captured:
                    usage_metadata = getattr(response, "usage_metadata", None)
                    if usage_metadata is not None:
                        token_fields = {
                            "input_tokens": getattr(
                                usage_metadata,
                                "prompt_token_count",
                                None,
                            ),
                            "output_tokens": getattr(
                                usage_metadata,
                                "candidates_token_count",
                                None,
                            ),
                            "total_tokens": getattr(
                                usage_metadata,
                                "total_token_count",
                                None,
                            ),
                        }
                        _response_usage_metadata.set(
                            {
                                name: value
                                for name, value in token_fields.items()
                                if isinstance(value, int) and not isinstance(value, bool)
                            }
                        )
                    _response_processing_started_at.set(time.perf_counter())
                    usage_metadata_captured = True

                text = getattr(response, "text", None)
                if isinstance(text, str) and text:
                    yield text
        finally:
            logger.info(
                "gemini_api_completed attempt=%d duration_ms=%d",
                attempt,
                round((time.perf_counter() - api_started_at) * 1000),
            )
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    pass

    async def _call_model(self, prompt: str, attempt: int) -> str:
        """Call Gemini and return a non-empty JSON response body."""
        api_started_at = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=0.2,
                ),
            )
            usage_metadata = getattr(response, "usage_metadata", None)
            if usage_metadata is not None:
                token_fields = {
                    "input_tokens": getattr(
                        usage_metadata,
                        "prompt_token_count",
                        None,
                    ),
                    "output_tokens": getattr(
                        usage_metadata,
                        "candidates_token_count",
                        None,
                    ),
                    "total_tokens": getattr(
                        usage_metadata,
                        "total_token_count",
                        None,
                    ),
                }
                _response_usage_metadata.set(
                    {
                        name: value
                        for name, value in token_fields.items()
                        if isinstance(value, int) and not isinstance(value, bool)
                    }
                )
            _response_processing_started_at.set(time.perf_counter())
        finally:
            logger.info(
                "gemini_api_completed attempt=%d duration_ms=%d",
                attempt,
                round((time.perf_counter() - api_started_at) * 1000),
            )

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, dict):
            return json.dumps(parsed)

        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Gemini returned an empty response")
        return text

    def _to_result(
        self,
        parsed: GeminiRawResponse,
        note_text: str,
    ) -> AnalysisResult:
        conditions = [
            self._to_condition(condition, note_text)
            for condition in parsed.conditions
        ]
        gaps = [self._to_gap(gap) for gap in parsed.gaps]

        return AnalysisResult(
            conditions=conditions,
            gaps=gaps,
            summary=parsed.summary,
            model_version=self._settings.gemini_model,
            prompt_version=PROMPT_VERSION,
        )

    @staticmethod
    def _to_condition(
        condition: ConditionFromModel,
        note_text: str,
    ) -> Condition:
        evidence_quote = condition.evidence_quote
        return Condition(
            id=str(uuid.uuid4()),
            condition_name=condition.condition_name,
            evidence_quote=evidence_quote,
            documentation_status=condition.documentation_status,
            suggested_icd10=condition.suggested_icd10,
            confidence=condition.confidence,
            quote_verified=verify_evidence_quote(evidence_quote, note_text),
        )

    @staticmethod
    def _to_gap(gap: DocumentationGapFromModel) -> DocumentationGap:
        return DocumentationGap(
            description=gap.description,
            related_condition=gap.related_condition,
        )


@lru_cache(maxsize=1)
def get_gemini_client() -> GeminiClient:
    """Return the process-wide Gemini client instance."""
    return GeminiClient(get_settings())
