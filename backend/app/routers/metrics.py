"""Aggregation logic for clinician-correction metrics."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.analysis import Analysis, ConditionReviewStatus, Review


@dataclass
class ConditionCorrectionStats:
    """Correction counts for one AI-extracted condition."""

    condition_name: str
    times_extracted: int = 0
    times_accepted: int = 0
    times_edited: int = 0
    times_rejected: int = 0

    @property
    def correction_rate(self) -> float:
        """Return the fraction of extractions that were corrected."""
        if self.times_extracted == 0:
            return 0.0

        corrected = self.times_edited + self.times_rejected
        return corrected / self.times_extracted


@dataclass
class ConditionAddedStats:
    """Count of conditions added by the clinician."""

    condition_name: str
    times_added: int = 0


@dataclass
class MetricsSummary:
    """Aggregated clinician-correction metrics."""

    reviews_analyzed: int
    condition_corrections: list[ConditionCorrectionStats] = field(
        default_factory=list
    )
    conditions_added_by_clinician: list[ConditionAddedStats] = field(
        default_factory=list
    )


def compute_condition_correction_metrics(
    reviews_with_analyses: list[tuple[Review, Analysis | None]],
) -> MetricsSummary:
    """Aggregate correction and addition metrics across reviews."""
    corrections_by_name: dict[str, ConditionCorrectionStats] = {}
    added_by_name: dict[str, ConditionAddedStats] = {}
    reviews_analyzed = 0

    for review, analysis in reviews_with_analyses:
        if analysis is None:
            continue

        reviews_analyzed += 1

        original_conditions_by_id = {
            condition.id: condition
            for condition in analysis.conditions
        }

        for condition_review in review.conditions:
            if condition_review.status == ConditionReviewStatus.ADDED:
                stats = added_by_name.setdefault(
                    condition_review.condition_name,
                    ConditionAddedStats(
                        condition_name=condition_review.condition_name
                    ),
                )
                stats.times_added += 1
                continue

            original = original_conditions_by_id.get(
                condition_review.source_condition_id
            )

            if original is None:
                continue

            stats = corrections_by_name.setdefault(
                original.condition_name,
                ConditionCorrectionStats(
                    condition_name=original.condition_name
                ),
            )

            stats.times_extracted += 1

            if condition_review.status == ConditionReviewStatus.ACCEPTED:
                stats.times_accepted += 1
            elif condition_review.status == ConditionReviewStatus.EDITED:
                stats.times_edited += 1
            elif condition_review.status == ConditionReviewStatus.REJECTED:
                stats.times_rejected += 1

    return MetricsSummary(
        reviews_analyzed=reviews_analyzed,
        condition_corrections=sorted(
            corrections_by_name.values(),
            key=lambda stats: stats.condition_name,
        ),
        conditions_added_by_clinician=sorted(
            added_by_name.values(),
            key=lambda stats: stats.condition_name,
        ),
    )