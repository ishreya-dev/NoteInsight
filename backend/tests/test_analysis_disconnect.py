"""SSE disconnect and active-analysis lifecycle tests.

Covers GAP-1: an active analysis job must not remain stuck in `processing`
when the SSE client disconnects mid-analysis.
"""

from __future__ import annotations

import asyncio  # noqa: E408 — used by test_timeout_failure_preserves_timeout_reason
from typing import Awaitable, Protocol, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from app.routers import notes as notes_router
from app.services.gemini_client import GeminiAnalysisError
from tests.conftest import TEST_USER, _parse_sse, make_note


class _TestAsyncStream(Protocol):
    def __anext__(self) -> Awaitable[str]: ...
    async def aclose(self) -> None: ...


def _make_request_scope(path: str = "/notes/n1/analysis/stream") -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }


def _make_settings(timeout: int = 60) -> MagicMock:
    return MagicMock(analysis_timeout_seconds=timeout, gemini_model="gemini-test")


@pytest.mark.asyncio
async def test_client_disconnect_during_streaming_marks_job_interrupted(
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consume a token during active analysis, then close the generator.

    The job must be marked ``failed`` with ``error_reason='interrupted'``.
    """
    caplog.set_level("WARNING")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: The patient"
        yield " presents with diabetes"
        # Generator is closed by the test here (simulates client disconnect).

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    first_event = await iterator.__anext__()
    assert "event: status" in first_event
    assert '"stage": "preparing"' in first_event

    second_event = await iterator.__anext__()
    assert "event: token" in second_event

    # Simulate the client disconnecting mid-stream.
    await iterator.aclose()

    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="failed",
        error_message="Analysis was interrupted. Please try again.",
        error_reason="interrupted",
    )
    assert any(
        "interrupted (client disconnect)" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_successful_analysis_does_not_mark_job_interrupted(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the analysis completes normally, the ``finally`` cleanup must not
    overwrite the completed job status."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    gemini_payload = (
        "SUMMARY: Follow-up visit.\n"
        'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up visit."}'
    )

    async def streaming_chunks(*_a, **_kw):
        for char in gemini_payload:
            yield char

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: complete" in e for e in events)

    # finish_analysis_job must have been called exactly once: by
    # _persist_and_finish with status "completed", NOT again by the finally.
    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="completed",
        analysis_id=db.persist_analysis_for_note.await_args.args[0].id,
        error_message=None,
        error_reason=None,
    )
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_gemini_failure_preserves_original_reason(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GeminiAnalysisError is raised, the existing handler controls the
    failure reason. The finally block must not overwrite with 'interrupted'."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: incomplete..."
        raise GeminiAnalysisError("bad output", failure_reason="invalid_output")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    parsed = _parse_sse("\n\n".join(events))
    error_events = [e for e in parsed if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "invalid_output"
    assert not any(e["event"] == "complete" for e in parsed)

    # finish_analysis_job called once by the handler with the specific reason,
    # not a second time with 'interrupted'.
    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "invalid_output"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_timeout_failure_preserves_timeout_reason(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the deadline is exceeded, the failure reason must remain 'timeout'
    and no duplicate 'interrupted' update should occur."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        # Exceed the deadline immediately on the next consumer check.
        raise asyncio.TimeoutError()

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(timeout=60),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e and '"reason": "timeout"' in e for e in events)

    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="failed",
        analysis_id=db.persist_analysis_for_note.await_args.args[0].id,
        error_message="Analysis failed. Please try again.",
        error_reason="timeout",
    )
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_gemini_analysis_error_persistence_failure_still_emits_error(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _persist_and_finish fails after a GeminiAnalysisError, the SSE error
    event must still be emitted and the job must not be marked interrupted."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: incomplete..."
        raise GeminiAnalysisError("bad output", failure_reason="invalid_output")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks
    db.finish_analysis_job.side_effect = RuntimeError("firestore unavailable")

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any(
        "event: error" in e and '"reason": "invalid_output"' in e for e in events
    )

    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "invalid_output"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_timeout_error_persistence_failure_still_emits_error(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _persist_and_finish fails after a TimeoutError, the SSE error event
    must still be emitted and the job must not be marked interrupted."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise asyncio.TimeoutError()

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks
    db.finish_analysis_job.side_effect = RuntimeError("firestore unavailable")

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(timeout=60),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e and '"reason": "timeout"' in e for e in events)

    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "timeout"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_generic_exception_persistence_failure_still_emits_error(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _persist_and_finish fails after a generic Exception, the SSE error
    event must still be emitted and the job must not be marked interrupted."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks
    db.finish_analysis_job.side_effect = RuntimeError("firestore unavailable")

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    parsed = _parse_sse("\n\n".join(events))
    error_events = [e for e in parsed if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "unknown"
    assert not any(e["event"] == "complete" for e in parsed)

    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "unknown"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_gemini_failure_with_persist_failure_marks_job_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the failed-analysis persist itself fails (not the finish step), the
    job must still reach a terminal ``failed`` state with the original reason,
    never remaining ``processing``."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.persist_analysis_for_note.side_effect = RuntimeError("firestore down")

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: incomplete..."
        raise GeminiAnalysisError("bad output", failure_reason="invalid_output")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any(
        "event: error" in e and '"reason": "invalid_output"' in e for e in events
    )
    # Fallback finish must run exactly once with the original reason.
    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "invalid_output"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_timeout_failure_with_persist_failure_marks_job_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guarantee as the Gemini case, for the timeout failure path."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.persist_analysis_for_note.side_effect = RuntimeError("firestore down")

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise asyncio.TimeoutError()

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(timeout=60),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass
    parsed = _parse_sse("\n\n".join(events))
    error_events = [e for e in parsed if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["data"]["reason"] == "timeout"
    assert not any(e["event"] == "complete" for e in parsed)

    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "timeout"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_generic_failure_with_persist_failure_marks_job_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guarantee as the Gemini case, for the generic exception path."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.persist_analysis_for_note.side_effect = RuntimeError("firestore down")

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e and '"reason": "unknown"' in e for e in events)
    call_kwargs = db.finish_analysis_job.await_args.kwargs
    assert call_kwargs["status"] == "failed"
    assert call_kwargs["error_reason"] == "unknown"
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_cleanup_failure_is_swallowed(
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the interrupt-cleanup ``finish_analysis_job`` call itself fails, the
    generator must not raise — the original ``GeneratorExit`` must propagate
    cleanly so the async generator closes."""
    caplog.set_level("ERROR")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: partial"

    def failing_finish(*_a, **_kw):
        raise RuntimeError("firestore unavailable during cleanup")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks
    db.finish_analysis_job.side_effect = failing_finish

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    await iterator.__anext__()  # status
    await iterator.__anext__()  # token
    await iterator.aclose()

    assert any(
        "Failed to mark interrupted job" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_disconnect_after_successful_persist_does_not_overwrite(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the generator is closed AFTER ``_persist_and_finish`` succeeds, the
    job must remain ``completed`` — not be flipped to ``interrupted``.

    Because ``job_completed = True`` is set before the ``complete`` yield,
    closing the generator after that point must not trigger the finally
    cleanup.
    """
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    gemini_payload = (
        "SUMMARY: Follow-up.\n"
        'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up."}'
    )

    async def streaming_chunks(*_a, **_kw):
        for char in gemini_payload:
            yield char

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    try:
        while True:
            event = await iterator.__anext__()
            # Close after the persist has completed (after "finalizing" status
            # has been generated) but before "complete" is consumed.
            if '"stage": "finalizing"' in event:
                await iterator.aclose()
                break
    except StopAsyncIteration:
        pass

    # finish_analysis_job was called once by _persist_and_finish with status
    # "completed". The finally block must NOT run because job_completed is True.
    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="completed",
        analysis_id=db.persist_analysis_for_note.await_args.args[0].id,
        error_message=None,
        error_reason=None,
    )
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


@pytest.mark.asyncio
async def test_disconnect_during_exact_cache_hit_does_not_overwrite(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When the analysis succeeds via the exact-cache path, closing the
    generator after the ``complete`` yield must not flip the job to
    ``interrupted``.
    """
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    # Firestore returns the cached analysis as a dict (from snapshot.to_dict()).
    db.get_cached_analysis_result.return_value = {
        "conditions": [],
        "gaps": [],
        "summary": "Cached follow-up.",
        "model_version": "gemini-test",
        "prompt_version": "v1",
    }

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    try:
        while True:
            event = await iterator.__anext__()
            if '"stage": "finalizing"' in event:
                await iterator.aclose()
                break
    except StopAsyncIteration:
        pass

    # finish_analysis_job called exactly once by _persist_and_finish.
    db.finish_analysis_job.assert_awaited_once_with(
        job_id="job-1",
        status="completed",
        analysis_id=db.persist_analysis_for_note.await_args.args[0].id,
        error_message=None,
        error_reason=None,
    )
    interrupted_calls = [
        c for c in db.finish_analysis_job.await_args_list
        if c.kwargs.get("error_reason") == "interrupted"
    ]
    assert interrupted_calls == []


def _make_gemini_streaming_payload() -> str:
    return (
        "SUMMARY: Follow-up visit.\n"
        'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up visit."}'
    )


async def _run_stream_to_completion(
    gemini: AsyncMock, db: AsyncMock, settings: MagicMock
) -> list[str]:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    payload = _make_gemini_streaming_payload()

    async def streaming_chunks(*_a, **_kw):
        for char in payload:
            yield char

    # Force the similar-cache lookup to miss via the db mock (no module
    # monkeypatch, to avoid leaking into other test modules).
    db.find_similar_cached_results.return_value = []

    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass
    return events


@pytest.mark.asyncio
async def test_persist_succeeds_finish_fails_preserves_successful_analysis(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When finish_analysis_job fails twice inside _finish_successful_analysis
    and the retry also fails, the already-persisted successful Analysis must be
    preserved, no failed replacement Analysis must be created, the job must be
    marked failed as a last resort, and the client must receive an error event—
    not a false complete event."""
    db.finish_analysis_job.side_effect = RuntimeError("firestore down")

    events = await _run_stream_to_completion(
        gemini, db, _make_settings()
    )

    assert any("event: error" in e for e in events)
    assert not any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False
    assert persisted.summary == "Follow-up visit."

    finish_calls = db.finish_analysis_job.await_args_list
    completed_calls = [c for c in finish_calls if c.kwargs.get("status") == "completed"]
    failed_calls = [c for c in finish_calls if c.kwargs.get("status") == "failed"]
    assert len(completed_calls) == 3
    assert len(failed_calls) == 1
    assert all(call.kwargs.get("analysis_id") == persisted.id for call in completed_calls)
    failed_call = failed_calls[0]
    assert failed_call.kwargs.get("error_reason") == "finalization_failed"


@pytest.mark.asyncio
async def test_both_finish_attempts_fail_does_not_mark_job_interrupted(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When finish_analysis_job fails three times (initial + retry + _retry_finish),
    the already-persisted successful Analysis must be preserved, the job must
    NOT be marked ``interrupted`` by the finally block, and instead the job is
    marked ``failed`` with ``finalization_failed``."""
    db.finish_analysis_job.side_effect = RuntimeError("firestore down")

    events = await _run_stream_to_completion(
        gemini, db, _make_settings()
    )

    assert any("event: error" in e for e in events)
    assert not any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False
    assert persisted.summary == "Follow-up visit."

    finish_calls = db.finish_analysis_job.await_args_list
    completed_calls = [c for c in finish_calls if c.kwargs.get("status") == "completed"]
    failed_calls = [c for c in finish_calls if c.kwargs.get("status") == "failed"]
    assert len(completed_calls) == 3
    assert len(failed_calls) == 1
    assert all(call.kwargs.get("analysis_id") == persisted.id for call in completed_calls)
    failed_call = failed_calls[0]
    assert failed_call.kwargs.get("error_reason") == "finalization_failed"


@pytest.mark.asyncio
async def test_persist_succeeds_finish_retry_succeeds_job_completed(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When the finish retry succeeds, the job becomes completed and references
    the successful Analysis: no failed Analysis and no stuck processing."""
    # First finish attempt fails, retry succeeds.
    db.finish_analysis_job.side_effect = [RuntimeError("transient"), None]

    events = await _run_stream_to_completion(
        gemini, db, _make_settings()
    )

    assert any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False

    # The successful retry marks the job completed with the good analysis_id.
    last = db.finish_analysis_job.await_args_list[-1]
    assert last.kwargs["status"] == "completed"
    assert last.kwargs["analysis_id"] == persisted.id


@pytest.mark.asyncio
async def test_first_finish_fails_retry_succeeds_job_completed(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When the first finish_analysis_job fails but the retry succeeds, the job
    must be marked completed with the successful Analysis ID and the client must
    receive a complete event."""
    db.finish_analysis_job.side_effect = [RuntimeError("transient"), None]

    events = await _run_stream_to_completion(
        gemini, db, _make_settings()
    )

    assert any("event: complete" in e for e in events)
    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False

    finish_calls = db.finish_analysis_job.await_args_list
    assert len(finish_calls) == 2
    assert all(call.kwargs.get("analysis_id") == persisted.id for call in finish_calls)
    assert finish_calls[-1].kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_both_finish_attempts_fail_retry_succeeds_job_completed(
    db: AsyncMock,
    gemini: AsyncMock,
) -> None:
    """When both finish_analysis_job attempts inside _finish_successful_analysis
    fail but the subsequent retry succeeds, the job must be marked completed
    with the successful Analysis ID and the client must receive a complete
    event. No second Analysis must be created."""
    db.finish_analysis_job.side_effect = [
        RuntimeError("transient"),
        RuntimeError("transient2"),
        None,
    ]

    events = await _run_stream_to_completion(
        gemini, db, _make_settings()
    )

    assert any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False

    finish_calls = db.finish_analysis_job.await_args_list
    assert len(finish_calls) == 3
    assert all(call.kwargs.get("analysis_id") == persisted.id for call in finish_calls)
    assert finish_calls[-1].kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_both_finish_attempts_fail_marks_job_failed_and_preserves_analysis(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When finish_analysis_job fails for all completed attempts, the successful
    Analysis must be preserved, note.latest_analysis_id must remain unchanged,
    no complete event must be emitted, and the job must be marked failed
    instead of being left processing or interrupted."""
    old_analysis_id = "a-old"
    note = make_note(note_id="n1").model_copy(update={
        "analysis_job_id": "job-1",
        "latest_analysis_id": old_analysis_id,
    })
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.finish_analysis_job.side_effect = RuntimeError("firestore down")

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: Follow-up.\n"
        yield 'DATA:{"conditions": [], "gaps": [], "summary": "Follow-up."}'

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e for e in events)
    assert not any("event: complete" in e for e in events)

    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is False
    assert note.latest_analysis_id == old_analysis_id

    finish_calls = db.finish_analysis_job.await_args_list
    completed_calls = [c for c in finish_calls if c.kwargs.get("status") == "completed"]
    failed_calls = [c for c in finish_calls if c.kwargs.get("status") == "failed"]
    assert len(completed_calls) == 3
    assert len(failed_calls) == 1
    assert all(call.kwargs.get("analysis_id") == persisted.id for call in completed_calls)
    assert failed_calls[0].kwargs.get("error_reason") == "finalization_failed"


@pytest.mark.asyncio
async def test_reanalysis_failure_preserves_previous_latest_analysis_id(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a re-analysis fails, the note's previous successful Analysis ID
    must be preserved as ``latest_analysis_id``."""
    old_analysis_id = "a-old"
    note = make_note(note_id="n1").model_copy(update={
        "analysis_job_id": "job-1",
        "latest_analysis_id": old_analysis_id,
    })
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    async def persist_and_update(analysis, *, condition_count: int, latest_analysis_id=None):
        note.latest_analysis_id = latest_analysis_id

    db.persist_analysis_for_note.side_effect = persist_and_update

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e for e in events)
    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is True
    assert note.latest_analysis_id == old_analysis_id

    finish_calls = db.finish_analysis_job.await_args_list
    failed_calls = [c for c in finish_calls if c.kwargs.get("status") == "failed"]
    assert len(failed_calls) == 1
    assert failed_calls[0].kwargs["error_reason"] == "unknown"


@pytest.mark.asyncio
async def test_new_note_failure_sets_latest_analysis_id_to_failed(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a new note (no previous Analysis) fails, the failed Analysis ID
    becomes ``latest_analysis_id``."""
    note = make_note(note_id="n1").model_copy(update={
        "analysis_job_id": "job-1",
        "latest_analysis_id": None,
    })
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None

    async def fake_similar(*_a, **_kw):
        return None

    async def streaming_chunks(*_a, **_kw):
        yield "SUMMARY: starting..."
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(notes_router, 'find_similar_cached_analysis', fake_similar)
    gemini.stream_analyze_note = streaming_chunks

    async def persist_and_update(analysis, *, condition_count: int, latest_analysis_id=None):
        note.latest_analysis_id = latest_analysis_id

    db.persist_analysis_for_note.side_effect = persist_and_update

    response = await notes_router.stream_analysis(
        note.id,
        Request(_make_request_scope()),
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=_make_settings(),
    )

    iterator = cast(_TestAsyncStream, response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: error" in e for e in events)
    assert db.persist_analysis_for_note.await_count == 1
    persisted = db.persist_analysis_for_note.await_args.args[0]
    assert persisted.is_failed is True
    assert note.latest_analysis_id == persisted.id

    finish_calls = db.finish_analysis_job.await_args_list
    failed_calls = [c for c in finish_calls if c.kwargs.get("status") == "failed"]
    assert len(failed_calls) == 1
    assert failed_calls[0].kwargs["error_reason"] == "unknown"
