"""Background execution for note analysis jobs."""

from __future__ import annotations

import json
import logging
import time
import uuid

from app.config import Settings
from app.models.analysis import Analysis, AnalysisJobStatus, Condition, DocumentationGap, GeminiRawResponse
from app.models.note import Note
from app.services.firestore_client import (
    DocumentConflictError,
    DocumentDataError,
    FirestoreClient,
)
from app.services.firestore_codec import hash_note_text
from app.services.gemini_client import (
    AnalysisResult,
    GeminiAnalysisError,
    GeminiClient,
    PROMPT_VERSION,
    strip_markdown_fences,
    _normalize_response_payload,
    verify_evidence_quote,
)

logger = logging.getLogger(__name__)


async def stream_analysis_and_persist(
    note: Note,
    db: FirestoreClient,
    gemini: GeminiClient,
    settings: Settings,
) -> Analysis:
    analysis_id = str(uuid.uuid4())
    cache_key = "{}:{}:{}".format(
        hash_note_text(note.raw_text),
        PROMPT_VERSION,
        settings.gemini_model,
    )

    result = None
    try:
        result = await _get_cached_result(db, cache_key)
        if result is None:
            full_text_parts: list[str] = []
            async for chunk in gemini.stream_analyze_note(note.raw_text):
                full_text_parts.append(chunk)
            raw_text = "".join(full_text_parts)
            cleaned = strip_markdown_fences(raw_text)
            if not cleaned:
                raise ValueError("Gemini returned an empty response")
            try:
                parsed_obj = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise ValueError("Gemini returned malformed JSON") from exc
            normalized = _normalize_response_payload(parsed_obj)
            parsed = GeminiRawResponse.model_validate(normalized)
            result = _analysis_result_from_parsed(parsed, note.raw_text, settings)
            await _store_cached_result(db, cache_key, result)
    except GeminiAnalysisError:
        raise
    except Exception as exc:
        raise GeminiAnalysisError(
            "Analysis processing failed", failure_reason="unknown"
        ) from exc

    if result is None:
        raise GeminiAnalysisError(
            "Analysis did not produce a result", failure_reason="invalid_output"
        )

    analysis = Analysis(
        id=analysis_id,
        note_id=note.id,
        user_id=note.user_id,
        conditions=tuple(result.conditions),
        gaps=tuple(result.gaps),
        summary=result.summary,
        model_version=result.model_version,
        prompt_version=result.prompt_version,
        is_failed=False,
        failure_reason=None,
    )
    return analysis


async def _run_analysis_job(
    note: Note,
    job_id: str,
    db: FirestoreClient,
    gemini: GeminiClient,
    settings: Settings,
) -> None:
    started_at = time.perf_counter()
    logger.info("analysis_job_started job_id=%s note_id=%s", job_id, note.id)
    try:
        analysis = await _analyze_and_persist(note, db, gemini, settings)
        await db.finish_analysis_job(
            job_id=job_id,
            status=(
                AnalysisJobStatus.FAILED.value
                if analysis.is_failed
                else AnalysisJobStatus.COMPLETED.value
            ),
            analysis_id=analysis.id,
            error_message=(
                "Analysis failed. Please try again."
                if analysis.is_failed
                else None
            ),
            error_reason=analysis.failure_reason if analysis.is_failed else None,
        )
    except Exception:
        logger.exception("Analysis job failed for note %s", note.id)
        try:
            await db.finish_analysis_job(
                job_id=job_id,
                status=AnalysisJobStatus.FAILED.value,
                error_message="Analysis failed. Please try again.",
                error_reason="unknown",
            )
        except Exception:
            logger.exception("Unable to mark analysis job %s as failed", job_id)
    finally:
        logger.info(
            "analysis_job_completed job_id=%s note_id=%s total_duration_ms=%d",
            job_id,
            note.id,
            round((time.perf_counter() - started_at) * 1000),
        )


async def _analyze_and_persist(
    note: Note,
    db: FirestoreClient,
    gemini: GeminiClient,
    settings: Settings,
) -> Analysis:
    """Run Gemini (or reuse a cached result), persist, and point the note at it."""
    analysis_id = str(uuid.uuid4())
    # Include the prompt version and model in the cache key, not just the
    # note text, so improving the prompt or switching models invalidates
    # previously cached results instead of silently reusing stale output.
    cache_key = "{}:{}:{}".format(
        hash_note_text(note.raw_text),
        PROMPT_VERSION,
        settings.gemini_model,
    )

    try:
        result = await _get_cached_result(db, cache_key)
        if result is None:
            result = await gemini.analyze_note(note.raw_text)
            await _store_cached_result(db, cache_key, result)

        analysis = Analysis(
            id=analysis_id,
            note_id=note.id,
            user_id=note.user_id,
            conditions=tuple(result.conditions),
            gaps=tuple(result.gaps),
            summary=result.summary,
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            is_failed=False,
            failure_reason=None,
        )
    except GeminiAnalysisError as exc:
        logger.warning("Gemini analysis failed for note %s", note.id)
        analysis = _create_failed_analysis(
            analysis_id=analysis_id,
            note=note,
            settings=settings,
            failure_reason=exc.failure_reason,
        )
    except Exception:
        logger.exception("Unexpected analysis failure for note %s", note.id)
        analysis = _create_failed_analysis(
            analysis_id=analysis_id,
            note=note,
            settings=settings,
            failure_reason="unknown",
        )

    condition_count = 0 if analysis.is_failed else len(analysis.conditions)

    persist_started_at = time.perf_counter()
    try:
        await db.persist_analysis_for_note(
            analysis,
            condition_count=condition_count,
        )
    except (PermissionError, LookupError):
        raise
    except DocumentConflictError:
        logger.exception("Analysis ID conflict for note %s", note.id)
        raise
    except DocumentDataError:
        logger.exception("Corrupt data while saving analysis for note %s", note.id)
        raise
    except Exception:
        logger.exception("Unexpected failure saving analysis for note %s", note.id)
        raise
    finally:
        logger.info(
            "persist_analysis_for_note duration_ms=%d",
            round((time.perf_counter() - persist_started_at) * 1000),
        )

    return analysis


def _create_failed_analysis(
    analysis_id: str,
    note: Note,
    settings: Settings,
    failure_reason: str,
) -> Analysis:
    return Analysis(
        id=analysis_id,
        note_id=note.id,
        user_id=note.user_id,
        conditions=(),
        gaps=(),
        summary="Analysis could not be completed. Please try again.",
        model_version=settings.gemini_model,
        prompt_version=PROMPT_VERSION,
        is_failed=True,
        failure_reason=failure_reason[:2000],
    )


async def _get_cached_result(
    db: FirestoreClient,
    cache_key: str,
) -> AnalysisResult | None:
    """Return a cached Gemini result for identical note text, if any.

    The cache is a pure optimization: any failure to read it must fall
    through to a normal (uncached) Gemini call rather than fail the job.
    """
    try:
        cached = await db.get_cached_analysis_result(cache_key)
    except Exception:
        logger.exception("Failed to read analysis cache for key %s", cache_key)
        return None

    if cached is None:
        return None

    try:
        conditions = [
            Condition(id=str(uuid.uuid4()), **condition)
            for condition in cached["conditions"]
        ]
        gaps = [DocumentationGap(**gap) for gap in cached["gaps"]]
        return AnalysisResult(
            conditions=conditions,
            gaps=gaps,
            summary=cached["summary"],
            model_version=cached["model_version"],
            prompt_version=cached["prompt_version"],
        )
    except (KeyError, TypeError, ValueError):
        logger.exception(
            "Corrupt analysis cache entry for key %s; ignoring", cache_key
        )
        return None


async def _store_cached_result(
    db: FirestoreClient,
    cache_key: str,
    result: AnalysisResult,
) -> None:
    """Best-effort write of a fresh Gemini result to the shared cache."""
    payload = {
        "conditions": [
            condition.model_dump(mode="json", exclude={"id"})
            for condition in result.conditions
        ],
        "gaps": [gap.model_dump(mode="json") for gap in result.gaps],
        "summary": result.summary,
        "model_version": result.model_version,
        "prompt_version": result.prompt_version,
    }
    try:
        await db.cache_analysis_result(cache_key, payload)
    except Exception:
        logger.exception("Failed to write analysis cache for key %s", cache_key)


def _analysis_result_from_parsed(
    parsed: GeminiRawResponse,
    note_text: str,
    settings: Settings,
) -> AnalysisResult:
    conditions = [
        Condition(
            id=str(uuid.uuid4()),
            condition_name=condition.condition_name,
            evidence_quote=condition.evidence_quote,
            documentation_status=condition.documentation_status,
            suggested_icd10=condition.suggested_icd10,
            confidence=condition.confidence,
            quote_verified=verify_evidence_quote(condition.evidence_quote, note_text),
        )
        for condition in parsed.conditions
    ]
    gaps = [DocumentationGap(description=gap.description, related_condition=gap.related_condition) for gap in parsed.gaps]
    return AnalysisResult(
        conditions=conditions,
        gaps=gaps,
        summary=parsed.summary,
        model_version=settings.gemini_model,
        prompt_version=PROMPT_VERSION,
    )