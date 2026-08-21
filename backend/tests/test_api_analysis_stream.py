"""Analysis progress stream behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from tests.conftest import make_analysis, make_note


def test_analysis_stream_emits_progress_and_complete(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    analysis = make_analysis(note_id=note.id)
    db.get_note.return_value = note
    db.get_analysis.return_value = analysis
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        {"status": "processing", "user_id": note.user_id},
        {"status": "completed", "user_id": note.user_id, "analysis_id": analysis.id},
    ]
    db.claim_analysis_job.return_value = "processing"

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: status" in response.text
    assert '"stage": "preparing"' in response.text
    assert '"stage": "analyzing_conditions"' in response.text
    assert "event: complete" in response.text
    assert f'"note_id": "{note.id}"' in response.text
    db.persist_analysis_for_note.assert_awaited_once()
    gemini.analyze_note.assert_awaited_once()
    db.finish_analysis_job.assert_awaited_once()


def test_completed_analysis_reconnect_does_not_start_gemini_again(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    analysis = make_analysis(note_id=note.id)
    db.get_note.return_value = note
    db.get_analysis.return_value = analysis
    db.get_analysis_job.return_value = {
        "status": "completed",
        "user_id": note.user_id,
        "analysis_id": analysis.id,
    }

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: complete" in response.text
    db.claim_analysis_job.assert_not_awaited()
    gemini.analyze_note.assert_not_awaited()


def test_analysis_stream_rejects_note_not_owned(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_note.return_value = None

    response = client.get("/notes/not-owned/analysis/stream")

    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"
