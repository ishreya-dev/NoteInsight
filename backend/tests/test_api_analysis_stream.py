"""Analysis progress stream behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.routers import notes as notes_router
from app.services.gemini_client import GeminiAnalysisError
from tests.conftest import TEST_USER, _parse_sse, make_analysis, make_note


def test_analysis_stream_emits_progress_and_complete(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
) -> None:
    caplog.set_level("INFO")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.find_similar_cached_results.return_value = []

    stream_calls = 0

    async def stream_chunks(*_args, **_kwargs):
        nonlocal stream_calls
        stream_calls += 1
        yield "SUMMARY: Follow-up visit.\n"
        yield 'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up visit."}'

    gemini.stream_analyze_note = stream_chunks

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    events = _parse_sse(response.text)
    event_names = [e["event"] for e in events]
    assert "status" in event_names
    assert "token" in event_names
    assert "complete" in event_names

    preparing = next(e for e in events if e["event"] == "status" and e["data"].get("stage") == "preparing")
    finalizing = next(e for e in events if e["event"] == "status" and e["data"].get("stage") == "finalizing")
    complete = next(e for e in events if e["event"] == "complete")
    assert events.index(preparing) < events.index(finalizing) < events.index(complete)
    assert complete["data"]["note_id"] == note.id
    db.persist_analysis_for_note.assert_awaited_once()
    assert stream_calls == 1
    db.finish_analysis_job.assert_awaited_once()
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "analysis_stream_completed" in message
        and "duration_ms=" in message
        and "poll_count=0" in message
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

    stream_calls = 0

    async def noop_stream(*_args, **_kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return
        yield  # pragma: no cover

    gemini.stream_analyze_note = noop_stream

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert any(e["event"] == "complete" for e in events)
    assert not any(e["event"] == "token" for e in events)
    complete_events = [e for e in events if e["event"] == "complete"]
    assert complete_events[0]["data"]["analysis"]["id"] == analysis.id
    db.claim_analysis_job.assert_not_awaited()
    assert stream_calls == 0


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

    stream_calls = 0

    async def noop_stream(*_args, **_kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return
        yield  # pragma: no cover

    gemini.stream_analyze_note = noop_stream

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert any(e["event"] == "complete" for e in events)
    assert not any(e["event"] == "token" for e in events)
    complete_events = [e for e in events if e["event"] == "complete"]
    assert complete_events[0]["data"]["analysis"]["id"] == analysis.id
    db.claim_analysis_job.assert_not_awaited()
    assert stream_calls == 0


@pytest.mark.asyncio
async def test_two_sse_streams_schedule_only_the_claim_winner(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        {"status": "processing", "user_id": note.user_id},
        {"status": "completed", "user_id": note.user_id, "analysis_id": "a1"},
        {"status": "completed", "user_id": note.user_id, "analysis_id": "a1"},
    ]
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.find_similar_cached_results.return_value = []
    db.get_analysis.return_value = make_analysis(note_id=note.id)

    stream_calls = 0

    async def stream_chunks(*_args, **_kwargs):
        nonlocal stream_calls
        stream_calls += 1
        yield "SUMMARY: Follow-up.\n"
        yield 'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up."}'

    gemini.stream_analyze_note = stream_chunks

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
        settings=MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test"),
    )
    second_response = await notes_router.stream_analysis(
        note.id,
        Request(request_scope),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test"),
    )

    first_body = cast(AsyncIterator[str], first_response.body_iterator)
    await first_body.__anext__()
    await first_body.__anext__()
    second_body = cast(AsyncIterator[str], second_response.body_iterator)
    await second_body.__anext__()

    assert stream_calls == 1
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
    events = _parse_sse(response.text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "unknown"
    assert error_events[0]["data"]["message"] == "Analysis status could not be loaded."
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
    events = _parse_sse(response.text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "unknown"
    assert error_events[0]["data"]["message"] == "Analysis could not be started."
    assert "firestore unavailable" not in response.text


def test_missing_job_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
) -> None:
    """When the analysis job does not exist, the client receives a safe error."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = None

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    events = _parse_sse(response.text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "unknown"
    assert error_events[0]["data"]["message"] == "Analysis could not be found."


def test_job_polling_failure_emits_safe_sse_error(
    client: TestClient,
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.side_effect = [
        {"status": "pending", "user_id": note.user_id},
        RuntimeError("firestore unavailable"),
    ]
    db.claim_analysis_job.return_value = "processing"

    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    response = client.get(f"/notes/{note.id}/analysis/stream")

    assert response.status_code == 200
    events = _parse_sse(response.text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "unknown"
    assert error_events[0]["data"]["message"] == "Analysis status could not be loaded."
    assert stream_calls == 0
    db.persist_analysis_for_note.assert_not_called()
    assert not any(e["event"] == "complete" for e in events)
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
    assert '"reason": "unknown"' in response.text
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
    assert '"reason": "unknown"' in response.text


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
    events = _parse_sse(response.text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "rate_limited"
    assert error_events[0]["data"]["message"] == "Analysis failed. Please try again."


@pytest.mark.asyncio
async def test_gemini_failure_persists_failed_analysis_and_marks_job_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
) -> None:
    caplog.set_level("INFO")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    notes_router.find_similar_cached_analysis = fake_similar

    async def failing_stream(*_a, **_kw):
        raise GeminiAnalysisError(
            "provider failure",
            failure_reason="rate_limited",
        )
        yield  # pragma: no cover

    gemini.stream_analyze_note = failing_stream

    response = await notes_router.stream_analysis(
        note.id,
        Request({
            "type": "http",
            "method": "GET",
            "path": "/notes/n1/analysis/stream",
            "headers": [],
            "query_string": b"",
        }),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test"),
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    parsed = _parse_sse("\n\n".join(events))
    error_events = [e for e in parsed if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "rate_limited"
    assert not any(e["event"] == "complete" for e in parsed)

    db.persist_analysis_for_note.assert_awaited_once()
    persisted_analysis = db.persist_analysis_for_note.await_args.args[0]
    assert persisted_analysis.is_failed is True
    assert persisted_analysis.failure_reason == "rate_limited"
    failed_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("status") == "failed"
    ]
    assert len(failed_calls) == 1
    assert failed_calls[0].kwargs["error_reason"] == "rate_limited"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "analysis_stream_completed" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_analysis_persistence_failure_marks_job_failed_safely(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    notes_router.find_similar_cached_analysis = fake_similar
    db.persist_analysis_for_note.side_effect = RuntimeError("private firestore detail")
    db.finish_analysis_job.side_effect = RuntimeError("private firestore detail")

    async def stream_chunks(*_a, **_kwargs):
        yield "SUMMARY: Follow-up.\n"
        yield 'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up."}'

    gemini.stream_analyze_note = stream_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request({
            "type": "http",
            "method": "GET",
            "path": "/notes/n1/analysis/stream",
            "headers": [],
            "query_string": b"",
        }),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test"),
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e for e in events)
    assert not any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    failed_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("status") == "failed"
    ]
    assert len(failed_calls) >= 1
    assert failed_calls[0].kwargs["error_reason"] == "finalization_failed"


@pytest.mark.asyncio
async def test_job_completion_failure_is_handled_without_raising(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    notes_router.find_similar_cached_analysis = fake_similar

    async def stream_chunks(*_a, **_kwargs):
        yield "SUMMARY: Follow-up.\n"
        yield 'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up."}'

    gemini.stream_analyze_note = stream_chunks
    db.finish_analysis_job.side_effect = RuntimeError("private firestore detail")

    response = await notes_router.stream_analysis(
        note.id,
        Request({
            "type": "http",
            "method": "GET",
            "path": "/notes/n1/analysis/stream",
            "headers": [],
            "query_string": b"",
        }),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test"),
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e for e in events)
    assert not any("event: complete" in e for e in events)

    db.persist_analysis_for_note.assert_awaited_once()
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False

    failed_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("status") == "failed"
    ]
    assert len(failed_calls) >= 1
    assert failed_calls[0].kwargs["error_reason"] == "finalization_failed"


def test_analysis_stream_rejects_note_not_owned(
    client: TestClient,
    db: AsyncMock,
) -> None:
    db.get_note.return_value = None

    response = client.get("/notes/not-owned/analysis/stream")

    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"
