"""Review create/update API behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.models.analysis import ConditionReviewStatus, Review
from app.services.firestore_codec import should_mark_note_reviewed_for_analysis
from tests.conftest import TEST_USER, make_analysis


async def _echo_upsert_review(**kwargs):
    """Persist-shaped mock: return a Review built from the upsert payload."""
    payload = kwargs["payload"]
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    review = Review(
        id=kwargs["analysis_id"],
        analysis_id=kwargs["analysis_id"],
        note_id=kwargs["note_id"],
        user_id=kwargs["user_id"],
        conditions=list(payload.conditions),
        gaps=list(payload.gaps),
        reviewer_notes=payload.notes,
        created_at=now,
        updated_at=now,
    )
    return review, True


def test_cannot_review_failed_analysis(client: TestClient, db: AsyncMock) -> None:
    db.get_analysis.return_value = make_analysis(failed=True)

    response = client.post(
        "/analyses/a1/reviews",
        json={"conditions": [], "gaps": []},
    )

    assert response.status_code == 400
    assert "failed" in response.json()["detail"].lower()
    db.upsert_review.assert_not_awaited()


def test_unknown_source_condition_is_rejected(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_analysis.return_value = make_analysis()

    response = client.post(
        "/analyses/a1/reviews",
        json={
            "conditions": [
                {
                    "source_condition_id": "unknown",
                    "condition_name": "Type 2 diabetes mellitus",
                    "evidence_quote": "Type 2 diabetes, controlled",
                    "documentation_status": "well_documented",
                    "suggested_icd10": "E11.9",
                    "status": "accepted",
                }
            ]
        },
    )

    assert response.status_code == 400
    assert "unknown condition" in response.json()["detail"].lower()


def test_duplicate_source_condition_is_rejected_by_request_validation(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_analysis.return_value = make_analysis()
    condition = {
        "source_condition_id": "c1",
        "condition_name": "Type 2 diabetes mellitus",
        "evidence_quote": "Type 2 diabetes, controlled",
        "documentation_status": "well_documented",
        "suggested_icd10": "E11.9",
        "status": "accepted",
    }

    response = client.post(
        "/analyses/a1/reviews",
        json={"conditions": [condition, {**condition, "status": "edited"}]},
    )

    assert response.status_code == 422


def test_create_review_returns_201(client: TestClient, db: AsyncMock) -> None:
    analysis = make_analysis()
    db.get_analysis.return_value = analysis
    review = Review(
        id=analysis.id,
        analysis_id=analysis.id,
        note_id=analysis.note_id,
        user_id=TEST_USER.uid,
        conditions=[],
        gaps=[],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.upsert_review.return_value = (review, True)

    response = client.post(
        f"/analyses/{analysis.id}/reviews",
        json={
            "conditions": [
                {
                    "source_condition_id": "c1",
                    "condition_name": "Type 2 diabetes mellitus",
                    "evidence_quote": "Type 2 diabetes, controlled",
                    "documentation_status": "well_documented",
                    "suggested_icd10": "E11.9",
                    "status": "accepted",
                }
            ]
        },
    )

    assert response.status_code == 201
    assert db.upsert_review.await_args.kwargs["condition_count"] == 1


def test_update_review_returns_200(client: TestClient, db: AsyncMock) -> None:
    analysis = make_analysis()
    db.get_analysis.return_value = analysis
    review = Review(
        id=analysis.id,
        analysis_id=analysis.id,
        note_id=analysis.note_id,
        user_id=TEST_USER.uid,
        conditions=[],
        gaps=[],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    db.upsert_review.return_value = (review, False)

    response = client.put(
        f"/analyses/{analysis.id}/reviews",
        json={"conditions": [], "gaps": []},
    )

    assert response.status_code == 200


def test_all_rejected_conditions_yield_zero_condition_count(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis()
    db.get_analysis.return_value = analysis
    review = Review(
        id=analysis.id,
        analysis_id=analysis.id,
        note_id=analysis.note_id,
        user_id=TEST_USER.uid,
        conditions=[],
        gaps=[],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.upsert_review.return_value = (review, True)

    response = client.post(
        f"/analyses/{analysis.id}/reviews",
        json={
            "conditions": [
                {
                    "source_condition_id": "c1",
                    "condition_name": "Type 2 diabetes mellitus",
                    "evidence_quote": "Type 2 diabetes, controlled",
                    "documentation_status": "well_documented",
                    "suggested_icd10": "E11.9",
                    "status": ConditionReviewStatus.REJECTED.value,
                }
            ]
        },
    )

    assert response.status_code == 201
    assert db.upsert_review.await_args.kwargs["condition_count"] == 0


def test_create_review_rejects_other_users_analysis(
    client: TestClient,
    db: AsyncMock,
) -> None:
    owner_id = "user-a"
    # Client fixture authenticates as TEST_USER (user-1), distinct from owner.
    owner_analysis = make_analysis(
        analysis_id="a-owned-by-a",
        user_id=owner_id,
    )

    async def get_analysis(analysis_id: str, user_id: str):
        if analysis_id == owner_analysis.id and user_id == owner_id:
            return owner_analysis
        return None

    db.get_analysis.side_effect = get_analysis

    response = client.post(
        f"/analyses/{owner_analysis.id}/reviews",
        json={
            "conditions": [
                {
                    "source_condition_id": "c1",
                    "condition_name": "Type 2 diabetes mellitus",
                    "evidence_quote": "Type 2 diabetes, controlled",
                    "documentation_status": "well_documented",
                    "suggested_icd10": "E11.9",
                    "status": "accepted",
                }
            ]
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Analysis not found"
    db.upsert_review.assert_not_awaited()
    db.get_analysis.assert_awaited_once_with(owner_analysis.id, TEST_USER.uid)


def test_create_review_preserves_edited_condition(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis()
    db.get_analysis.return_value = analysis
    db.upsert_review.side_effect = _echo_upsert_review

    edited_name = "Type 2 diabetes mellitus with hyperglycemia"
    edited_icd10 = "E11.65"
    edited_quote = "Type 2 diabetes with hyperglycemia"

    response = client.post(
        f"/analyses/{analysis.id}/reviews",
        json={
            "conditions": [
                {
                    "source_condition_id": "c1",
                    "condition_name": edited_name,
                    "evidence_quote": edited_quote,
                    "documentation_status": "ambiguous",
                    "suggested_icd10": edited_icd10,
                    "status": ConditionReviewStatus.EDITED.value,
                }
            ]
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["conditions"]) == 1
    condition = body["conditions"][0]
    assert condition["status"] == ConditionReviewStatus.EDITED.value
    assert condition["source_condition_id"] == "c1"
    assert condition["condition_name"] == edited_name
    assert condition["evidence_quote"] == edited_quote
    assert condition["suggested_icd10"] == edited_icd10
    assert condition["documentation_status"] == "ambiguous"
    assert db.upsert_review.await_args.kwargs["condition_count"] == 1


def test_create_review_preserves_added_condition(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis()
    db.get_analysis.return_value = analysis
    db.upsert_review.side_effect = _echo_upsert_review

    added_name = "Essential hypertension"
    added_quote = "BP elevated; hypertension addressed"
    added_icd10 = "I10"

    response = client.post(
        f"/analyses/{analysis.id}/reviews",
        json={
            "conditions": [
                {
                    "condition_name": added_name,
                    "evidence_quote": added_quote,
                    "documentation_status": "well_documented",
                    "suggested_icd10": added_icd10,
                    "status": ConditionReviewStatus.ADDED.value,
                }
            ]
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["conditions"]) == 1
    condition = body["conditions"][0]
    assert condition["status"] == ConditionReviewStatus.ADDED.value
    assert condition.get("source_condition_id") is None
    assert condition["condition_name"] == added_name
    assert condition["evidence_quote"] == added_quote
    assert condition["suggested_icd10"] == added_icd10
    assert db.upsert_review.await_args.kwargs["condition_count"] == 1


def test_reviewing_old_analysis_does_not_mark_latest_note_reviewed() -> None:
    assert not should_mark_note_reviewed_for_analysis(
        {"latest_analysis_id": "a-new"},
        "a-old",
    )
    assert should_mark_note_reviewed_for_analysis(
        {"latest_analysis_id": "a-old"},
        "a-old",
    )
