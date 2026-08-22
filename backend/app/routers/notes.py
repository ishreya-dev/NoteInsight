"""Note submission and history endpoints."""

from __future__ import annotations

import logging
import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from google.api_core.exceptions import FailedPrecondition
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.dependencies import enforce_analysis_rate_limit, get_current_user
from app.models.analysis import Analysis, Review
from app.models.note import Note, NoteCreate, NoteListItem
from app.models.user import AuthenticatedUser
from app.services.analysis_jobs import _run_analysis_job
from app.services.firestore_client import (
    DocumentConflictError,
    DocumentDataError,
    FirestoreClient,
    get_firestore_client,
)
from app.services.gemini_client import (
    GeminiClient,
    get_gemini_client,
)
from app.services.analysis_jobs import stream_analysis_and_persist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteCreateResponse(BaseModel):
    """Note plus the identifier for its asynchronous analysis job."""

    note: Note
    job_id: str


class NoteDetailResponse(BaseModel):
    """Note with optional latest analysis and review."""

    note: Note
    analysis: Analysis | None = None
    review: Review | None = None


class NoteHistoryResponse(BaseModel):
    """Authenticated user's note history."""

    items: list[NoteListItem] = Field(default_factory=list)


@router.post(
    "",
    response_model=NoteCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(enforce_analysis_rate_limit)],
)
async def create_note(
    payload: NoteCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> NoteCreateResponse:
    """Create a note and enqueue its analysis."""
    try:
        note = await db.create_note(
            user_id=current_user.uid,
            note_id=str(uuid.uuid4()),
            payload=payload,
        )
    except DocumentConflictError as exc:
        logger.error("Note ID conflict during create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Note could not be created",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected failure creating note")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Note could not be created",
        ) from exc

    job_id = str(uuid.uuid4())
    try:
        await db.create_analysis_job(
            job_id=job_id, note_id=note.id, user_id=note.user_id
        )
    except (PermissionError, LookupError) as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    except Exception as exc:
        logger.exception("Unexpected failure creating analysis job")
        raise HTTPException(
            status_code=500, detail="Note could not be created"
        ) from exc
    return NoteCreateResponse(note=note.model_copy(update={"analysis_job_id": job_id}), job_id=job_id)


@router.get("", response_model=NoteHistoryResponse)
async def list_notes(
    limit: int = Query(default=50, ge=1, le=100),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> NoteHistoryResponse:
    """Return the caller's notes, newest first."""
    try:
        items = await db.list_notes(
            user_id=current_user.uid,
            limit=limit,
        )
    except FailedPrecondition as exc:
        logger.error(
            "Firestore FailedPrecondition listing notes: "
            "type=%s message=%s code=%s details=%s",
            type(exc).__name__,
            str(exc),
            getattr(exc, "code", None),
            getattr(exc, "details", None),
            exc_info=True,
        )
        if "requires an index" not in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Notes could not be loaded",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Notes are temporarily unavailable because the required "
                "Firestore index is missing or still building."
            ),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected failure listing notes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Notes could not be loaded",
        ) from exc

    return NoteHistoryResponse(items=items)


@router.get("/{note_id}", response_model=NoteDetailResponse)
async def get_note(
    note_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> NoteDetailResponse:
    """Return a note with its latest analysis and review when present."""
    note = await _require_owned_note(
        db=db,
        note_id=note_id,
        user_id=current_user.uid,
    )

    analysis: Analysis | None = None
    review: Review | None = None

    if note.latest_analysis_id:
        try:
            analysis = await db.get_analysis(
                analysis_id=note.latest_analysis_id,
                user_id=current_user.uid,
            )
            if analysis is not None:
                review = await db.get_review_for_analysis(
                    analysis_id=analysis.id,
                    user_id=current_user.uid,
                )
        except DocumentDataError as exc:
            logger.error("Corrupt analysis/review for note '%s'", note.id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Note analysis could not be loaded",
            ) from exc
        except Exception:
            logger.exception(
                "Transient error loading analysis/review for note '%s'", note.id
            )
            analysis = None
            review = None

    return NoteDetailResponse(
        note=note,
        analysis=analysis,
        review=review,
    )


@router.post(
    "/{note_id}/analyze",
    response_model=NoteCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(enforce_analysis_rate_limit)],
)
async def reanalyze_note(
    note_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> NoteCreateResponse:
    """Create a new analysis for an existing note; prior analyses are kept."""
    note = await _require_owned_note(
        db=db,
        note_id=note_id,
        user_id=current_user.uid,
    )

    job_id = str(uuid.uuid4())
    try:
        await db.create_analysis_job(
            job_id=job_id, note_id=note.id, user_id=current_user.uid
        )
    except (PermissionError, LookupError) as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    except Exception as exc:
        logger.exception("Unexpected failure creating analysis job")
        raise HTTPException(
            status_code=500, detail="Analysis could not be started"
        ) from exc
    return NoteCreateResponse(
        note=note.model_copy(update={"analysis_job_id": job_id}), job_id=job_id
    )


@router.get("/{note_id}/analysis/stream")
async def stream_analysis(
    note_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
    gemini: GeminiClient = Depends(get_gemini_client),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream status for the note's single, persisted analysis job."""
    note = await _require_owned_note(db, note_id, current_user.uid)
    if not note.analysis_job_id:
        raise HTTPException(status_code=404, detail="Analysis job not found")

    job_id = note.analysis_job_id

    async def events() -> AsyncIterator[str]:
        stream_started_at = time.perf_counter()
        poll_count = 0

        def log_stream_completed() -> None:
            logger.info(
                "analysis_stream_completed note_id=%s job_id=%s "
                "duration_ms=%d poll_count=%d",
                note_id,
                job_id,
                round((time.perf_counter() - stream_started_at) * 1000),
                poll_count,
            )

        try:
            job = await db.get_analysis_job(
                job_id=job_id,
                user_id=current_user.uid,
            )
        except Exception:
            logger.exception("Failed to load analysis job %s", job_id)
            yield _sse(
                "error",
                {"message": "Analysis status could not be loaded."},
            )
            log_stream_completed()
            return
        if job is None:
            yield _sse("error", {"message": "Analysis could not be found."})
            log_stream_completed()
            return

        if job.get("status") == "pending":
            try:
                claimed = await db.claim_analysis_job(
                    job_id=job_id,
                    user_id=current_user.uid,
                )
            except Exception:
                logger.exception("Failed to claim analysis job %s", job_id)
                yield _sse(
                    "error",
                    {"message": "Analysis could not be started."},
                )
                log_stream_completed()
                return
            if claimed == "claimed":
                deadline_at = time.perf_counter() + settings.analysis_timeout_seconds
                yield _sse("status", {"stage": "preparing", "message": "Preparing clinical analysis..."})
                try:
                    analysis = None
                    async for chunk in gemini.stream_analyze_note(note.raw_text):
                        if time.perf_counter() > deadline_at:
                            raise asyncio.TimeoutError()
                        yield _sse("token", {"text": chunk})
                    analysis = await stream_analysis_and_persist(note, db, gemini, settings)
                except GeminiAnalysisError as exc:
                    analysis = _create_failed_analysis(
                        note=note,
                        settings=settings,
                        failure_reason=exc.failure_reason,
                    )
                    await _persist_and_finish(db, note, job_id, analysis)
                    yield _sse("error", {
                        "reason": exc.failure_reason,
                        "message": "Analysis failed. Please try again.",
                    })
                    log_stream_completed()
                    return
                except asyncio.TimeoutError:
                    await _persist_and_finish(db, note, job_id, _create_failed_analysis(
                        note=note,
                        settings=settings,
                        failure_reason="timeout",
                    ))
                    yield _sse("error", {
                        "reason": "timeout",
                        "message": "Analysis took too long. Please try again.",
                    })
                    log_stream_completed()
                    return
                except Exception:
                    logger.exception("Analysis job failed for note %s", note.id)
                    await _persist_and_finish(db, note, job_id, _create_failed_analysis(
                        note=note,
                        settings=settings,
                        failure_reason="unknown",
                    ))
                    yield _sse("error", {
                        "reason": "unknown",
                        "message": "Analysis failed. Please try again.",
                    })
                    log_stream_completed()
                    return

                yield _sse("status", {"stage": "finalizing", "message": "Finalizing analysis..."})
                yield _sse("complete", {"note_id": note_id, "analysis": analysis.model_dump(mode="json")})
                log_stream_completed()
                return

        yield _sse("status", {"stage": "preparing", "message": "Preparing clinical analysis..."})
        last_status = None
        while True:
            if await request.is_disconnected():
                log_stream_completed()
                return
            poll_count += 1
            try:
                job = await db.get_analysis_job(
                    job_id=job_id,
                    user_id=current_user.uid,
                )
            except Exception:
                logger.exception("Failed to poll analysis job %s", job_id)
                yield _sse(
                    "error",
                    {"message": "Analysis status could not be loaded."},
                )
                log_stream_completed()
                return
            if job is None:
                yield _sse("error", {"message": "Analysis could not be found."})
                log_stream_completed()
                return
            current_status = job.get("status")
            if current_status != last_status:
                if current_status == "processing":
                    yield _sse("status", {"stage": "analyzing_conditions", "message": "Analyzing conditions and documentation gaps..."})
                last_status = current_status
            if current_status == "completed":
                try:
                    analysis = await db.get_analysis(
                        analysis_id=job["analysis_id"],
                        user_id=current_user.uid,
                    )
                except Exception:
                    logger.exception(
                        "Failed to read completed analysis for job %s", job_id
                    )
                    yield _sse(
                        "error",
                        {"message": "Analysis could not be loaded."},
                    )
                    log_stream_completed()
                    return
                if analysis is None:
                    yield _sse("error", {"message": "Analysis could not be loaded."})
                    log_stream_completed()
                    return
                yield _sse("status", {"stage": "finalizing", "message": "Finalizing analysis..."})
                yield _sse("complete", {"note_id": note_id, "analysis": analysis.model_dump(mode="json")})
                log_stream_completed()
                return
            if current_status == "failed":
                yield _sse(
                    "error",
                    {
                        "reason": job.get("error_reason", "unknown"),
                        "message": job.get(
                            "error_message",
                            "Analysis failed. Please try again.",
                        ),
                    },
                )
                log_stream_completed()
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _persist_and_finish(
    db: FirestoreClient,
    note: Note,
    job_id: str,
    analysis: Analysis,
) -> None:
    condition_count = 0 if analysis.is_failed else len(analysis.conditions)
    try:
        await db.persist_analysis_for_note(
            analysis,
            condition_count=condition_count,
        )
    except Exception:
        logger.exception("Failed to persist analysis for note %s", note.id)
    try:
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
        logger.exception("Failed to finish analysis job %s", job_id)


def _create_failed_analysis(
    note: Note,
    settings: Settings,
    failure_reason: str,
) -> Analysis:
    return Analysis(
        id=str(uuid.uuid4()),
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


async def _require_owned_note(
    db: FirestoreClient,
    note_id: str,
    user_id: str,
) -> Note:
    try:
        note = await db.get_note(note_id=note_id, user_id=user_id)
    except DocumentDataError as exc:
        logger.error("Corrupt note document '%s'", note_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Note could not be loaded",
        ) from exc

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )
    return note