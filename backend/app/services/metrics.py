"""Clinician-correction metrics endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.models.analysis import Analysis, Review
from app.models.user import AuthenticatedUser
from app.services.firestore_client import (
    DocumentDataError,
    FirestoreClient,
    get_firestore_client,
)
from app.services.metrics import compute_condition_correction_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/metrics", tags=["metrics"])


class ConditionCorrectionMetric(BaseModel):
    """Correction counts for one AI-extracted condition."""

    condition_name: str
    times_extracted: int
    times_accepted: int
    times_edited: int
    times_rejected: int
    correction_rate: float = Field(
        description=(
            "Fraction of extractions edited or rejected by the clinician."
        )
    )


class ConditionAddedMetric(BaseModel):
    """Count of conditions added by the clinician."""

    condition_name: str
    times_added: int


class MetricsResponse(BaseModel):
    """Per-user clinician correction metrics."""

    reviews_analyzed: int
    condition_corrections: list[ConditionCorrectionMetric] = Field(
        default_factory=list
    )
    conditions_added_by_clinician: list[ConditionAddedMetric] = Field(
        default_factory=list
    )


@router.get("/conditions", response_model=MetricsResponse)
async def get_condition_correction_metrics(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
) -> MetricsResponse:
    """Return clinician correction metrics for the authenticated user."""
    try:
        reviews = await db.list_reviews_for_user(
            user_id=current_user.uid
        )
    except DocumentDataError as exc:
        logger.exception(
            "Corrupt review data while computing metrics for user %s",
            current_user.uid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Metrics could not be computed",
        ) from exc
    except Exception as exc:
        logger.exception(
            "Failed to load reviews for metrics for user %s",
            current_user.uid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Metrics could not be computed",
        ) from exc

    reviews_with_analyses: list[tuple[Review, Analysis | None]] = []

    for review in reviews:
        try:
            analysis = await db.get_analysis(
                analysis_id=review.analysis_id,
                user_id=current_user.uid,
            )
        except DocumentDataError:
            logger.exception(
                "Corrupt analysis '%s' referenced by review '%s'; "
                "excluding from metrics",
                review.analysis_id,
                review.id,
            )
            analysis = None
        except Exception:
            logger.exception(
                "Failed to load analysis '%s' for metrics; "
                "excluding from metrics",
                review.analysis_id,
            )
            analysis = None

        reviews_with_analyses.append((review, analysis))

    summary = compute_condition_correction_metrics(
        reviews_with_analyses
    )

    return MetricsResponse(
        reviews_analyzed=summary.reviews_analyzed,
        condition_corrections=[
            ConditionCorrectionMetric(
                condition_name=stats.condition_name,
                times_extracted=stats.times_extracted,
                times_accepted=stats.times_accepted,
                times_edited=stats.times_edited,
                times_rejected=stats.times_rejected,
                correction_rate=stats.correction_rate,
            )
            for stats in summary.condition_corrections
        ],
        conditions_added_by_clinician=[
            ConditionAddedMetric(
                condition_name=stats.condition_name,
                times_added=stats.times_added,
            )
            for stats in summary.conditions_added_by_clinician
        ],
    )