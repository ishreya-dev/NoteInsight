"""Analysis retrieval API behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.services.firestore_client import DocumentDataError
from tests.conftest import make_analysis, make_review


def test_get_analysis_returns_analysis_and_optional_review(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis()
    review = make_review(analysis_id=analysis.id)
    db.get_analysis.return_value = analysis
    db.get_review_for_analysis.return_value = review

    response = client.get(f"/analyses/{analysis.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["analysis"]["id"] == analysis.id
    assert body["review"]["id"] == review.id


def test_get_analysis_missing_or_unauthorized_returns_404(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_analysis.return_value = None
    response = client.get("/analyses/a-other")
    assert response.status_code == 404
    assert response.json()["detail"] == "Analysis not found"


def test_get_failed_analysis_is_allowed(
    client: TestClient,
    db: AsyncMock,
) -> None:
    analysis = make_analysis(failed=True)
    db.get_analysis.return_value = analysis

    response = client.get(f"/analyses/{analysis.id}")

    assert response.status_code == 200
    assert response.json()["analysis"]["is_failed"] is True
    assert response.json()["review"] is None


def test_corrupt_stored_analysis_returns_safe_500(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_analysis.side_effect = DocumentDataError("Invalid Analysis document")

    response = client.get("/analyses/a1")

    assert response.status_code == 500
    assert response.json()["detail"] == "Analysis could not be loaded"
    assert "ValidationError" not in response.text
    assert "Invalid Analysis" not in response.text
