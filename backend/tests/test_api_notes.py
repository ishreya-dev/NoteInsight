"""Notes API behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock


from google.api_core.exceptions import FailedPrecondition
from fastapi.testclient import TestClient

from app.models.note import NoteListItem, ReviewStatus
from app.models.user import AuthenticatedUser
from tests.conftest import TEST_USER, make_analysis, make_note, make_review


def test_create_note_success(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    db.create_note.return_value = make_note()

    response = client.post(
        "/notes",
        json={
            "raw_text": "Patient has Type 2 diabetes, controlled on metformin.",
            "pseudonym": "Pt-A",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["note"]["analysis_job_id"] == body["job_id"]
    db.create_analysis_job.assert_awaited_once()
    gemini.analyze_note.assert_not_awaited()


def test_create_note_creates_job_without_calling_gemini(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    db.create_note.return_value = make_note()

    response = client.post(
        "/notes",
        json={
            "raw_text": "Patient has Type 2 diabetes, controlled on metformin.",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    db.create_analysis_job.assert_awaited_once()
    gemini.analyze_note.assert_not_awaited()


def test_reanalyze_updates_latest_analysis_pointer(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_note.return_value = make_note(
        latest_analysis_id="a-old",
        condition_count=2,
    )

    response = client.post("/notes/n1/analyze")

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["note"]["analysis_job_id"] == body["job_id"]


def test_get_missing_note_returns_404(client: TestClient, db: AsyncMock) -> None:
    db.get_note.return_value = None
    response = client.get("/notes/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"


def test_get_note_includes_latest_analysis_and_review(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis()
    note = make_note(latest_analysis_id=analysis.id, condition_count=1)
    review = make_review(analysis_id=analysis.id, note_id=note.id)
    db.get_note.return_value = note
    db.get_analysis.return_value = analysis
    db.get_review_for_analysis.return_value = review

    response = client.get(f"/notes/{note.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["analysis"]["id"] == analysis.id
    assert body["review"]["id"] == review.id


def test_list_notes_returns_history_items(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.list_notes.return_value = [
        NoteListItem(
            id="n1",
            pseudonym="Pt-A",
            visit_date=None,
            created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            review_status=ReviewStatus.PENDING,
            condition_count=1,
        )
    ]

    response = client.get("/notes?limit=10")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_list_notes_returns_only_current_users_notes_newest_first(
    client: TestClient,
    db: AsyncMock,
) -> None:
    user_a = TEST_USER
    user_b = AuthenticatedUser(uid="user-b", email="other@example.com")

    note_a_old = NoteListItem(
        id="n-a-old",
        pseudonym="Pt-A-old",
        visit_date=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        review_status=ReviewStatus.PENDING,
        condition_count=0,
    )
    note_a_new = NoteListItem(
        id="n-a-new",
        pseudonym="Pt-A-new",
        visit_date=None,
        created_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
        review_status=ReviewStatus.PENDING,
        condition_count=1,
    )
    note_b = NoteListItem(
        id="n-b",
        pseudonym="Pt-B",
        visit_date=None,
        created_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
        review_status=ReviewStatus.PENDING,
        condition_count=2,
    )

    # Ownership-aware store: filters by requesting user, newest-first.
    stored = [
        (user_a.uid, note_a_old),
        (user_a.uid, note_a_new),
        (user_b.uid, note_b),
    ]

    async def list_notes(*, user_id: str, limit: int = 50):
        owned = [item for owner, item in stored if owner == user_id]
        owned.sort(key=lambda item: item.created_at, reverse=True)
        return owned[:limit]

    db.list_notes.side_effect = list_notes

    response = client.get("/notes?limit=10")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["n-a-new", "n-a-old"]
    assert all(item["id"] != "n-b" for item in items)
    db.list_notes.assert_awaited_once_with(user_id=user_a.uid, limit=10)


def test_list_notes_returns_503_when_index_is_missing(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.list_notes.side_effect = FailedPrecondition(
        "The query requires an index. That index is currently building."
    )

    response = client.get("/notes?limit=10")

    assert response.status_code == 503
    assert "missing or still building" in response.json()["detail"]
    assert "currently building" not in response.text


def test_list_notes_returns_500_for_unrelated_failed_precondition(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.list_notes.side_effect = FailedPrecondition("database is unavailable")

    response = client.get("/notes?limit=10")

    assert response.status_code == 500
    assert response.json()["detail"] == "Notes could not be loaded"
    assert "database is unavailable" not in response.text


def test_reanalysis_preserves_previous_analysis(
    client: TestClient,
    db: AsyncMock,
) -> None:
    """Reanalysis creates a new analysis; the previous analysis remains retrievable."""
    old_analysis = make_analysis(analysis_id="a-old", note_id="n1")
    note = make_note(note_id="n1", latest_analysis_id=old_analysis.id)
    analyses = {old_analysis.id: old_analysis}

    async def persist_analysis_for_note(analysis, *, condition_count: int):
        analyses[analysis.id] = analysis
        note.latest_analysis_id = analysis.id
        note.condition_count = condition_count
        note.review_status = ReviewStatus.PENDING

    async def get_analysis(analysis_id: str, user_id: str):
        analysis = analyses.get(analysis_id)
        if analysis is None or analysis.user_id != user_id:
            return None
        return analysis

    async def get_note(note_id: str, user_id: str):
        if note.id != note_id or note.user_id != user_id:
            return None
        return note

    db.get_note.side_effect = get_note
    db.get_analysis.side_effect = get_analysis
    db.persist_analysis_for_note.side_effect = persist_analysis_for_note

    reanalyze = client.post(f"/notes/{note.id}/analyze")
    assert reanalyze.status_code == 202
    job_id = reanalyze.json()["job_id"]
    assert job_id

    old_response = client.get(f"/analyses/{old_analysis.id}")
    assert old_response.status_code == 200
    assert old_response.json()["analysis"]["id"] == old_analysis.id

    assert note.latest_analysis_id == old_analysis.id


def test_create_note_returns_safe_500_when_analysis_job_creation_fails(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.create_note.return_value = make_note()
    db.create_analysis_job.side_effect = RuntimeError("firestore down")

    response = client.post(
        "/notes",
        json={
            "raw_text": "Patient has Type 2 diabetes, controlled on metformin.",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Note could not be created"
    assert "firestore down" not in response.text
