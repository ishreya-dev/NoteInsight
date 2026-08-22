"""Background execution for note analysis jobs."""

from __future__ import annotations

import json
import logging
import time
import uuid

from pydantic import ValidationError

from app.config import Settings
from app.models.analysis import Analysis, AnalysisJobStatus, Condition, DocumentationGap, GeminiRawResponse
from app.models.note import Note
from app.services.firestore_client import (
    DocumentConflictError,
    DocumentDataError,
    FirestoreClient,
)
from app.services.firestore_codec import hash_note_text
from app.services.similarity import MinHash, build_shingles, compute_buckets, tokenize
from app.services.similarity_cache import (
    DEFAULT_SIMILARITY_THRESHOLD,
    select_best_similar_candidate,
)
from app.services.gemini_client import (
    AnalysisResult,
    GeminiAnalysisError,
    GeminiClient,
    PROMPT_VERSION,
    build_validation_retry_prompt,
    strip_markdown_fences,
    _normalize_response_payload,
    verify_evidence_quote,
    extract_json_after_data,
)

logger = logging.getLogger(__name__)


async def _stream_note(
    gemini: GeminiClient,
    note_text: str,
    request_contents: str | None = None,
) -> str:
    parts: list[str] = []
    async for chunk in gemini.stream_analyze_note(note_text, request_contents=request_contents):
        parts.append(chunk)
    return "".join(parts)


async def stream_analysis_and_persist(
    note: Note,
    db: FirestoreClient,
    gemini: GeminiClient,
    settings: Settings,
    raw_text: str | None = None,
) -> Analysis:
    analysis_id = str(uuid.uuid4())
    cache_key = "{}:{}:{}".format(
        hash_note_text(note.raw_text),
        PROMPT_VERSION,
        settings.gemini_model,
    )

    result = None
    try:
        if raw_text is None:
            result = await _get_cached_result(db, cache_key, note.raw_text)
        if result is None:
            if raw_text is None:
                raw_text = await _stream_note(gemini, note.raw_text)
            try:
                result = _parse_streamed_response(raw_text, note.raw_text, settings)
                await _store_cached_result(db, cache_key, result, note_text=note.raw_text)
            except GeminiAnalysisError as exc:
                if exc.failure_reason == "invalid_output":
                    try:
                        base_prompt = gemini._prompt_template.replace(
                            "{note_text}", note.raw_text
                        )
                        corrected_prompt = build_validation_retry_prompt(
                            base_prompt, exc
                        )
                        raw_text = await _stream_note(
                            gemini, note.raw_text, request_contents=corrected_prompt
                        )
                    except AttributeError:
                        raw_text = await _stream_note(gemini, note.raw_text)
                    result = _parse_streamed_response(raw_text, note.raw_text, settings)
                    await _store_cached_result(db, cache_key, result, note_text=note.raw_text)
                else:
                    raise
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
        result = await _get_cached_result(db, cache_key, note.raw_text)
        if result is None:
            similar = await find_similar_cached_analysis(db, note.raw_text)
            if similar is not None:
                try:
                    result = _parse_cached_result(similar, note.raw_text)
                except (KeyError, TypeError, ValueError):
                    logger.exception(
                        "Corrupt similar-cache entry for note %s; ignoring", note.id
                    )
                    result = None
        if result is None:
            result = await gemini.analyze_note(note.raw_text)
            await _store_cached_result(db, cache_key, result, note_text=note.raw_text)

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


def _parse_cached_result(cached: dict, note_text: str) -> AnalysisResult:
    """Build an ``AnalysisResult`` from a stored cache payload dict.

    Shared by the exact and similar cache paths; the candidate payload from a
    near-duplicate lookup has the same shape as an exact-cache entry.
    """
    conditions = [
        Condition(
            id=str(uuid.uuid4()),
            quote_verified=verify_evidence_quote(condition["evidence_quote"], note_text),
            **{key: value for key, value in condition.items() if key != "quote_verified"},
        )
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


async def _get_cached_result(
    db: FirestoreClient,
    cache_key: str,
    note_text: str,
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
        return _parse_cached_result(cached, note_text)
    except (KeyError, TypeError, ValueError):
        logger.exception(
            "Corrupt analysis cache entry for key %s; ignoring", cache_key
        )
        return None


async def _store_cached_result(
    db: FirestoreClient,
    cache_key: str,
    result: AnalysisResult,
    note_text: str,
) -> None:
    """Best-effort write of a fresh Gemini result to the shared cache.

    Stores the LSH buckets, MinHash signature, and word-shingle set derived
    from the note text so a future lookup can return this entry as a
    near-duplicate candidate and later compute an accurate lexical (Jaccard)
    similarity rather than relying on the approximate MinHash signature.
    """
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
    shingles = build_shingles(tokenize(note_text))
    signature = MinHash().signature(shingles)
    buckets = compute_buckets(note_text)
    try:
        await db.cache_analysis_result(
            cache_key,
            payload,
            buckets=buckets,
            signature=list(signature),
            shingles=list(shingles),
        )
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


def _parse_streamed_response(
    raw_text: str,
    note_text: str,
    settings: Settings,
) -> AnalysisResult:
    cleaned = extract_json_after_data(strip_markdown_fences(raw_text))
    if not cleaned:
        raise GeminiAnalysisError(
            "Gemini returned an empty response", failure_reason="invalid_output"
        )
    try:
        parsed_obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiAnalysisError(
            "Gemini returned malformed JSON", failure_reason="invalid_output"
        ) from exc
    normalized = _normalize_response_payload(parsed_obj)
    try:
        parsed = GeminiRawResponse.model_validate(normalized)
    except ValidationError as exc:
        raise GeminiAnalysisError(
            "Gemini returned invalid output", failure_reason="invalid_output"
        ) from exc
    return _analysis_result_from_parsed(parsed, note_text, settings)


async def find_similar_cached_analysis(
    db: FirestoreClient,
    note_text: str,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Return a safely reusable near-duplicate cached analysis, or ``None``.

    Service-layer orchestration of the similarity cache: derive LSH buckets
    from the note, fetch candidate entries from the global ``analysis_cache``
    collection, then apply the conservative safety decision. The cache stays
    global (no user/note scoping). When no candidate passes every safety
    check, returns ``None`` so the caller can fall back to Gemini. The exact
    cache lookup is handled separately and is not performed here.
    """
    buckets = compute_buckets(note_text)
    candidates = await db.find_similar_cached_results(buckets)
    return select_best_similar_candidate(note_text, candidates, threshold)
