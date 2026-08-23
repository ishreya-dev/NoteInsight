"""Analysis polling timeout tests.

Covers GAP-3: the polling loop must not run indefinitely while a job
remains `processing`. After a bounded number of polls derived from the
configured `analysis_timeout_seconds`, the poll should emit a safe SSE
error and exit WITHOUT modifying the job state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import MutableMapping, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from app.routers import notes as notes_router
from tests.conftest import TEST_USER, make_note


def _make_request_scope() -> dict:
    """Build an ASGI scope for the SSE stream endpoint."""
    return {
        "type": "http",
        "method": "GET",
        "path": "/notes/n1/analysis/stream",
        "headers": [],
        "query_string": b"",
    }


def _make_receive() -> Callable[[], Awaitable[MutableMapping[str, object]]]:
    """ASGI receive; Starlette uses this for disconnect detection only."""

    async def receive() -> MutableMapping[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _make_processing_job(user_id: str) -> dict:
    return {"status": "processing", "user_id": user_id}


def _make_connected_request(scope: dict, monkeypatch: pytest.MonkeyPatch) -> Request:
    """Build a Request whose is_disconnected() always returns False, so the
    polling loop relies on the count-based timeout.

    Starlette caches the disconnect result in ``_is_disconnected`` on the
    first call. We replace the instance attribute with a plain callable and
    reset the cached flag on every access so it always reports connected.
    """
    request = Request(scope, _make_receive())

    async def always_connected() -> bool:
        request._is_disconnected = False
        return False

    # Direct instance-attribute assignment works because Python's attribute
    # lookup checks the instance dict before the class.
    monkeypatch.setattr(request, "is_disconnected", always_connected)
    return request


@pytest.mark.asyncio
async def test_polling_timeout_emits_error(
    db: AsyncMock,
    gemini: AsyncMock,
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job stuck in `processing` causes the poll to time out and emit a
    safe SSE error."""
    # Set level to INFO so we capture both the WARNING timeout and the
    # INFO stream-completed log. caplog.set_level only affects future records.
    caplog.set_level("INFO")
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = _make_processing_job(note.user_id)
    # Use a short analysis timeout so max_polls is small and the test is fast.
    # analysis_timeout_seconds=0.6 -> max_polls = int(0.6/2/0.2) = 1
    settings = MagicMock(analysis_timeout_seconds=0.6, gemini_model="gemini-test")

    request = _make_connected_request(_make_request_scope(), monkeypatch)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any('"stage": "preparing"' in e for e in events)
    # The final event must be the timeout error.
    assert len(events) >= 2, f"Expected >= 2 events, got {len(events)}: {[e[:40] for e in events]}"
    assert "event: error" in events[-1], f"Last event has no error: {events[-1][:80]}"
    assert "Analysis is taking longer than expected" in events[-1]
    assert '"reason": "unknown"' in events[-1]
    # The timeout warning and the stream-completed info should both be logged.
    assert any("analysis_stream_completed" in r.getMessage() for r in caplog.records)
    assert any("Polling timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_polling_timeout_does_not_modify_job(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On polling timeout, the backend must NOT call finish_analysis_job.
    The job may still be actively processed by another worker."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = _make_processing_job(note.user_id)
    # Short timeout -> fast test.
    settings = MagicMock(analysis_timeout_seconds=0.6, gemini_model="gemini-test")

    request = _make_connected_request(_make_request_scope(), monkeypatch)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    try:
        while True:
            await iterator.__anext__()
    except StopAsyncIteration:
        pass

    db.finish_analysis_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_completes_before_timeout(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the job completes within the polling window, the normal `complete`
    event is emitted (no timeout error)."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    analysis = MagicMock()
    analysis.model_dump.return_value = {"id": "a1"}

    db.get_analysis_job.side_effect = [
        _make_processing_job(note.user_id),
        {"status": "completed", "user_id": note.user_id, "analysis_id": "a1"},
    ]
    db.get_analysis.return_value = analysis
    settings = MagicMock(analysis_timeout_seconds=60, gemini_model="gemini-test")

    request = _make_connected_request(_make_request_scope(), monkeypatch)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any("event: complete" in e for e in events)
    assert not any("taking longer than expected" in e for e in events)


@pytest.mark.asyncio
async def test_polling_disconnect_still_exits(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect must still exit the poll immediately, even with the new
    timeout check in place."""
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = _make_processing_job(note.user_id)
    # Short timeout so the test would eventually time out if disconnect
    # detection did not fire first.
    settings = MagicMock(analysis_timeout_seconds=0.6, gemini_model="gemini-test")

    request = Request(_make_request_scope(), _make_receive())

    async def always_disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", always_disconnected)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    events: list[str] = []
    try:
        while True:
            events.append(await iterator.__anext__())
    except StopAsyncIteration:
        pass

    assert any('"stage": "preparing"' in e for e in events)
    assert not any("taking longer than expected" in e for e in events)
    db.finish_analysis_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_limit_is_derived_from_settings(
    db: AsyncMock,
    gemini: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The max poll count is derived from `analysis_timeout_seconds`, not a
    fixed constant. A smaller timeout should produce a smaller poll budget.

    We verify by counting Firestore reads (one per poll) rather than SSE
    events, because most polls do not yield an event.
    """
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = _make_processing_job(note.user_id)

    # 10-second analysis timeout -> max_polls = int(10/2/0.2) = 25
    settings = MagicMock(analysis_timeout_seconds=10, gemini_model="gemini-test")

    request = _make_connected_request(_make_request_scope(), monkeypatch)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    try:
        while True:
            await iterator.__anext__()
    except StopAsyncIteration:
        pass

    # Each poll calls get_analysis_job once. With a 10s timeout the budget
    # is 25 polls. Allow a margin because the disconnect check runs first and
    # the timeout check runs before the read on the final iteration.
    assert 20 <= db.get_analysis_job.await_count <= 30, (
        f"Expected ~25 polls, got {db.get_analysis_job.await_count}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,expected_polls", [
    (0.39, 2),
    (0.4, 2),
    (0.79, 2),
    (0.8, 3),
])
async def test_polling_boundary_clamps_min_polls(
    db: AsyncMock,
    gemini: AsyncMock,
    timeout: float,
    expected_polls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    db.get_note.return_value = note
    db.get_analysis_job.return_value = _make_processing_job(note.user_id)
    settings = MagicMock(analysis_timeout_seconds=timeout, gemini_model="gemini-test")

    request = _make_connected_request(_make_request_scope(), monkeypatch)

    response = await notes_router.stream_analysis(
        note.id,
        request,
        current_user=TEST_USER,
        db=db,
        gemini=gemini,
        settings=settings,
    )

    iterator = cast(AsyncIterator[str], response.body_iterator)
    try:
        while True:
            await iterator.__anext__()
    except StopAsyncIteration:
        pass

    assert db.get_analysis_job.await_count == expected_polls
