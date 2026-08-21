"""Analysis retrieval and clinician review endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.dependencies import get_current_user
from app.models.analysis import (
    Analysis,
    ConditionReviewStatus,
    Review,
    ReviewCreate,
)
from app.models.user import AuthenticatedUser
from app.services.firestore_client import (
    DocumentDataError,
    FirestoreClient,
    get_firestore_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyses", tags=["analyses"])


class AnalysisDetailResponse(BaseModel):
    """Analysis with its optional clinician review."""

    analysis: Analysis
    review: Review | None = None


@router.get(
    "/{analysis_id}",
    response_model=AnalysisDetailResponse,
)
async def get_analysis(
    analysis_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> AnalysisDetailResponse:
    """Return an owned analysis and its review when present."""
    analysis = await _require_owned_analysis(
        db,
        analysis_id,
        current_user.uid,
    )

    try:
        review = await db.get_review_for_analysis(
            analysis.id,
            current_user.uid,
        )
    except DocumentDataError as exc:
        logger.error("Corrupt review for analysis '%s'", analysis.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Review could not be loaded",
        ) from exc

    return AnalysisDetailResponse(analysis=analysis, review=review)


@router.put("/{analysis_id}/reviews", response_model=Review)
@router.post(
    "/{analysis_id}/reviews",
    response_model=Review,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_review(
    analysis_id: str,
    payload: ReviewCreate,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> Review:
    """Create or update the single review for an analysis.

    Review document ID is ``analysis_id``. Note review state is updated in the
    same transaction only when this analysis is still the note's latest.
    """
    analysis = await _require_owned_analysis(
        db,
        analysis_id,
        current_user.uid,
    )

    if analysis.is_failed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot review a failed analysis",
        )

    _validate_review_references(payload, analysis)

    condition_count = sum(
        condition.status != ConditionReviewStatus.REJECTED
        for condition in payload.conditions
    )

    try:
        review, created = await db.upsert_review(
            analysis_id=analysis.id,
            note_id=analysis.note_id,
            user_id=current_user.uid,
            payload=payload,
            condition_count=condition_count,
        )
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        ) from exc
    except DocumentDataError as exc:
        logger.error("Corrupt data during review upsert for '%s'", analysis_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Review could not be saved",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected failure saving review for '%s'", analysis_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Review could not be saved",
        ) from exc

    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return review


async def _require_owned_analysis(
    db: FirestoreClient,
    analysis_id: str,
    user_id: str,
) -> Analysis:
    try:
        analysis = await db.get_analysis(analysis_id, user_id)
    except DocumentDataError as exc:
        logger.error("Corrupt analysis document '%s'", analysis_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analysis could not be loaded",
        ) from exc

    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found",
        )
    return analysis


def _validate_review_references(
    payload: ReviewCreate,
    analysis: Analysis,
) -> None:
    known_ids = {condition.id for condition in analysis.conditions}
    source_ids = [
        condition.source_condition_id
        for condition in payload.conditions
        if condition.source_condition_id is not None
    ]

    if set(source_ids) - known_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Review references an unknown condition",
        )

    if len(source_ids) != len(set(source_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A source condition cannot be reviewed more than once",
        )
