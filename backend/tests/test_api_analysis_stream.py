"""Analysis progress stream behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.routers import notes as notes_router
from app.services.analysis_jobs import _run_analysis_job
from app.services.gemini_client import GeminiAnalysisError
from tests.conftest import TEST_USER, make_analysis, make_note


def test_analysis_stream_emits_progress_and_complete(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
) -> None:
    caplog.set_level("INFO")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    analysis = make_analysis(note_id=note.id)
    db.get_note.return_value = note
    db.get_analysis.return_value = analysis
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        {"status": "processing", "user_id": note.user_id},
        {"status": "completed", "user_id": note.user_id, "analysis_id": analysis.id},
    ]
    db.claim_analysis_job.return_value = "claimed"

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
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "analysis_stream_completed" in message
        and "duration_ms=" in message
        and "poll_count=2" in message
        for message in messages
    )
    assert any(
        "analysis_job_completed" in message
        and "total_duration_ms=" in message
        for message in messages
    )
    assert any(
        "persist_analysis_for_note" in message
        and "duration_ms=" in message
        for message in messages
    )


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


def test_processing_analysis_stream_does_not_start_gemini_again(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    analysis = make_analysis(note_id=note.id)
    db.get_note.return_value = note
    db.get_analysis.return_value = analysis
    db.get_analysis_job.side_effect = [
        {"status": "processing", "user_id": note.user_id},
        {"status": "completed", "user_id": note.user_id, "analysis_id": analysis.id},
    ]

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: complete" in response.text
    db.claim_analysis_job.assert_not_awaited()
    gemini.analyze_note.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_sse_streams_schedule_only_the_claim_winner(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        {"status": "processing", "user_id": note.user_id},
    ]
    db.claim_analysis_job.return_value = "claimed"
    scheduled: list[object] = []

    def fake_create_task(coroutine: object) -> MagicMock:
        if hasattr(coroutine, "close"):
            coroutine.close()
        scheduled.append(coroutine)
        return MagicMock()

    monkeypatch.setattr(notes_router.asyncio, "create_task", fake_create_task)
    request_scope = {
        "type": "http",
        "method": "GET",
        "path": "/notes/n1/analysis/stream",
        "headers": [],
        "query_string": b"",
    }

    first_response = await notes_router.stream_analysis(
        note.id,
        Request(request_scope),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(),
    )
    second_response = await notes_router.stream_analysis(
        note.id,
        Request(request_scope),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(),
    )

    await first_response.body_iterator.__anext__()
    await second_response.body_iterator.__anext__()

    assert len(scheduled) == 1
    db.claim_analysis_job.assert_awaited_once()


def test_initial_job_lookup_failure_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.side_effect = RuntimeError("firestore credentials leaked")

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Analysis status could not be loaded." in response.text
    assert "firestore credentials leaked" not in response.text


def test_job_claim_failure_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.side_effect = RuntimeError("firestore unavailable")

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Analysis could not be started." in response.text
    assert "firestore unavailable" not in response.text


def test_job_polling_failure_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        RuntimeError("firestore unavailable"),
    ]
    db.claim_analysis_job.return_value = "claimed"

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Analysis status could not be loaded." in response.text
    assert "firestore unavailable" not in response.text


def test_completed_analysis_read_failure_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {
        "status": "completed",
        "user_id": note.user_id,
        "analysis_id": "a1",
    }
    db.get_analysis.side_effect = RuntimeError("private firestore detail")

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Analysis could not be loaded." in response.text
    assert "private firestore detail" not in response.text


def test_completed_job_with_missing_analysis_emits_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {
        "status": "completed",
        "user_id": note.user_id,
        "analysis_id": "a1",
    }
    db.get_analysis.return_value = None

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Analysis could not be loaded." in response.text


def test_failed_job_stream_includes_safe_failure_reason(
    client: TestClient,
    db: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {
        "status": "failed",
        "user_id": note.user_id,
        "error_reason": "rate_limited",
        "error_message": "Analysis failed. Please try again.",
    }

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    assert '"reason": "rate_limited"' in response.text
    assert "Analysis failed. Please try again." in response.text


@pytest.mark.asyncio
async def test_gemini_failure_persists_failed_analysis_and_marks_job_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
) -> None:
    caplog.set_level("INFO")
    gemini.analyze_note.side_effect = GeminiAnalysisError(
        "provider failure",
        failure_reason="rate_limited",
    )

    await _run_analysis_job(
        make_note(),
        "job-1",
        db,
        gemini,
        MagicMock(gemini_model="gemini-test"),
    )

    db.persist_analysis_for_note.assert_awaited_once()
    persisted_analysis = db.persist_analysis_for_note.await_args.args[0]
    assert persisted_analysis.is_failed is True
    assert persisted_analysis.failure_reason == "rate_limited"
    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="failed",
        analysis_id=persisted_analysis.id,
        error_message="Analysis failed. Please try again.",
        error_reason="rate_limited",
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any("analysis_job_started" in message for message in messages)
    assert any(
        "analysis_job_completed" in message
        and "total_duration_ms=" in message
        for message in messages
    )
    assert any(
        "persist_analysis_for_note" in message
        and "duration_ms=" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_analysis_persistence_failure_marks_job_failed_safely(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    db.persist_analysis_for_note.side_effect = RuntimeError("private firestore detail")

    await _run_analysis_job(
        make_note(),
        "job-1",
        db,
        gemini,
        MagicMock(gemini_model="gemini-test"),
    )

    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="failed",
        error_message="Analysis failed. Please try again.",
        error_reason="unknown",
    )


@pytest.mark.asyncio
async def test_job_completion_failure_is_handled_without_raising(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    db.finish_analysis_job.side_effect = RuntimeError("private firestore detail")

    await _run_analysis_job(
        make_note(),
        "job-1",
        db,
        gemini,
        MagicMock(gemini_model="gemini-test"),
    )

    assert db.finish_analysis_job.await_count == 2


def test_analysis_stream_rejects_note_not_owned(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_note.return_value = None

    response = client.get("/notes/not-owned/analysis/stream")

    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"
