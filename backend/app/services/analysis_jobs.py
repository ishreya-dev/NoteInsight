"""Background execution for note analysis jobs."""

from __future__ import annotations

import logging
import time
import uuid

from app.config import Settings
from app.models.analysis import Analysis
from app.models.note import Note
from app.services.firestore_client import (
    DocumentConflictError,
    DocumentDataError,
    FirestoreClient,
)
from app.services.gemini_client import (
    GeminiAnalysisError,
    GeminiClient,
    PROMPT_VERSION,
)

logger = logging.getLogger(__name__)


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
            status="failed" if analysis.is_failed else "completed",
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
                status="failed",
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
    """Run Gemini, persist the analysis, and point the note at it."""
    analysis_id = str(uuid.uuid4())

    try:
        result = await gemini.analyze_note(note.raw_text)
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
        logger.error("Analysis ID conflict for note %s", note.id)
        raise
    except DocumentDataError:
        logger.error("Corrupt data while saving analysis for note %s", note.id)
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