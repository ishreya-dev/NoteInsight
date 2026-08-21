"""Note submission and history endpoints."""

from __future__ import annotations

import logging
import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from google.api_core.exceptions import FailedPrecondition
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.dependencies import get_current_user
from app.models.analysis import Analysis, Review
from app.models.note import Note, NoteCreate, NoteListItem, ReviewStatus
from app.models.user import AuthenticatedUser
from app.services.firestore_client import (
    DocumentConflictError,
    DocumentDataError,
    FirestoreClient,
    get_firestore_client,
)
from app.services.gemini_client import (
    GeminiAnalysisError,
    GeminiClient,
    PROMPT_VERSION,
    get_gemini_client,
)

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
)
async def create_note(
    payload: NoteCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
    gemini: GeminiClient = Depends(get_gemini_client),
    settings: Settings = Depends(get_settings),
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

    return NoteDetailResponse(
        note=note,
        analysis=analysis,
        review=review,
    )


@router.post(
    "/{note_id}/analyze",
    response_model=NoteCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reanalyze_note(
    note_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
    gemini: GeminiClient = Depends(get_gemini_client),
    settings: Settings = Depends(get_settings),
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
        job = await db.get_analysis_job(job_id=job_id, user_id=current_user.uid)
        if job is None:
            yield _sse("error", {"message": "Analysis could not be found."})
            return

        if job.get("status") == "pending":
            claimed = await db.claim_analysis_job(
                job_id=job_id, user_id=current_user.uid
            )
            if claimed == "processing":
                asyncio.create_task(
                    _run_analysis_job(note, job_id, db, gemini, settings)
                )

        yield _sse("status", {"stage": "preparing", "message": "Preparing clinical analysis..."})
        last_status = None
        while True:
            if await request.is_disconnected():
                return
            job = await db.get_analysis_job(job_id=job_id, user_id=current_user.uid)
            if job is None:
                yield _sse("error", {"message": "Analysis could not be found."})
                return
            current_status = job.get("status")
            if current_status != last_status:
                if current_status == "processing":
                    yield _sse("status", {"stage": "analyzing_conditions", "message": "Analyzing conditions and documentation gaps..."})
                last_status = current_status
            if current_status == "completed":
                analysis = await db.get_analysis(
                    analysis_id=job["analysis_id"], user_id=current_user.uid
                )
                if analysis is None:
                    yield _sse("error", {"message": "Analysis could not be loaded."})
                    return
                yield _sse("status", {"stage": "finalizing", "message": "Finalizing analysis..."})
                yield _sse("complete", {"note_id": note_id, "analysis": analysis.model_dump(mode="json")})
                return
            if current_status == "failed":
                yield _sse("error", {"message": job.get("error_message", "Analysis failed. Please try again.")})
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _run_analysis_job(
    note: Note,
    job_id: str,
    db: FirestoreClient,
    gemini: GeminiClient,
    settings: Settings,
) -> None:
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
        )
    except Exception:
        logger.exception("Analysis job failed for note %s", note.id)
        await db.finish_analysis_job(
            job_id=job_id,
            status="failed",
            error_message="Analysis failed. Please try again.",
        )


def _note_after_analysis(note: Note, analysis: Analysis) -> Note:
    """Apply the post-persist note fields without an extra Firestore read."""
    return note.model_copy(
        update={
            "latest_analysis_id": analysis.id,
            "review_status": ReviewStatus.PENDING,
            "condition_count": (
                0 if analysis.is_failed else len(analysis.conditions)
            ),
        }
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
            failure_reason=str(exc),
        )
    except Exception:
        logger.exception("Unexpected analysis failure for note %s", note.id)
        analysis = _create_failed_analysis(
            analysis_id=analysis_id,
            note=note,
            settings=settings,
            failure_reason="Unexpected analysis service failure",
        )

    condition_count = 0 if analysis.is_failed else len(analysis.conditions)

    try:
        await db.persist_analysis_for_note(
            analysis,
            condition_count=condition_count,
        )
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        ) from exc
    except DocumentConflictError as exc:
        logger.error("Analysis ID conflict for note %s", note.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analysis could not be saved",
        ) from exc
    except DocumentDataError as exc:
        logger.error("Corrupt data while saving analysis for note %s", note.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analysis could not be saved",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected failure saving analysis for note %s", note.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analysis could not be saved",
        ) from exc

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
