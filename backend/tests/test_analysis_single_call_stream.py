"""Verification tests for the single-call streaming analysis flow."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.dependencies import get_current_user, get_firestore_client
from app.models.analysis import Condition, DocumentationStatus
from app.models.note import Note
from app.models.user import AuthenticatedUser
from app.services.analysis_jobs import _parse_streamed_response
from app.services.gemini_client import GeminiAnalysisError, PROMPT_VERSION, get_gemini_client
from app.services.similarity import build_shingles, tokenize
from app.config import get_settings
from tests.conftest import TEST_USER, make_analysis, make_note


@pytest.fixture
def app():
    from app.main import app as fastapi_app
    fastapi_app.dependency_overrides.clear()
    return fastapi_app


VALID_JSON = json.dumps({
    "conditions": [{
        "condition_name": "Diabetes",
        "evidence_quote": "Patient has diabetes",
        "documentation_status": "well_documented",
        "suggested_icd10": "E11.9",
        "confidence": 0.9,
    }],
    "gaps": [],
    "summary": "Patient has diabetes.",
})

CHUNKS = ["SUMMARY:\n", "Patient has diabetes.\n", "\nDATA:\n", VALID_JSON]


class FakeGemini:
    """Fake Gemini client that streams from a fixed list of chunks."""

    def __init__(self):
        self.call_count = 0
        self.chunks_yielded = 0

    def stream_analyze_note(self, note_text, deadline_at=None, request_contents=None):
        self.call_count += 1
        async def _stream():
            for chunk in CHUNKS:
                self.chunks_yielded += 1
                yield chunk
        return _stream()


@pytest.fixture
def note():
    n = make_note(note_id="n1")
    n.analysis_job_id = "job-1"
    return n


@pytest.fixture
def valid_db(note, db):
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.claim_analysis_job.return_value = "claimed"
    db.get_cached_analysis_result.return_value = None
    db.cache_analysis_result = AsyncMock()
    return db


@pytest.fixture
def valid_gemini():
    return FakeGemini()


@pytest.fixture
def client_with_stream(valid_db, valid_gemini):
    from app.main import app
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: valid_gemini
    app.dependency_overrides[get_firestore_client] = lambda: valid_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        etype = ""
        edata = ""
        for line in block.strip().split("\n"):
            if line.startswith("event: "):
                etype = line[7:]
            elif line.startswith("data: "):
                edata = line[6:]
        if etype and edata:
            events.append((etype, json.loads(edata)))
    return events


class TestSingleCallStreaming:
    """Verify the single-Gemini-call streaming flow."""

    def test_one_gemini_call_per_analysis(self, client_with_stream, valid_gemini):
        """Requirement: single Gemini call, not two."""
        client_with_stream.get("/notes/n1/analysis/stream")
        assert valid_gemini.call_count == 1

    def test_actual_chunks_reach_sse(self, client_with_stream, valid_gemini):
        """Requirement: actual Gemini chunks reach the SSE stream."""
        response = client_with_stream.get("/notes/n1/analysis/stream")
        text = response.text
        token_events = [
            data for etype, data in _parse_sse(text)
            if etype == "token"
        ]
        assert len(token_events) > 0
        token_text = "".join(e["text"] for e in token_events)
        assert "Patient has diabetes." in token_text

    def test_same_response_used_for_validation(self, client_with_stream):
        """Requirement: the same streamed response is used for final validation."""
        response = client_with_stream.get("/notes/n1/analysis/stream")
        events = _parse_sse(response.text)
        complete = [data for etype, data in events if etype == "complete"]
        assert len(complete) == 1
        analysis = complete[0]["analysis"]
        assert len(analysis["conditions"]) == 1
        assert analysis["conditions"][0]["condition_name"] == "Diabetes"
        assert analysis["summary"] == "Patient has diabetes."

    def test_analysis_persisted_on_success(self, client_with_stream, valid_db):
        """Requirement: successful analysis is persisted to Firestore."""
        client_with_stream.get("/notes/n1/analysis/stream")
        valid_db.persist_analysis_for_note.assert_awaited_once()

    def test_job_marked_completed_on_success(self, client_with_stream, valid_db):
        """Requirement: analysis job is marked completed on success."""
        client_with_stream.get("/notes/n1/analysis/stream")
        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert any(c.kwargs.get("status") == "completed" for c in finish_calls)

    def test_cache_hit_skips_gemini_and_emits_complete(self, valid_db, app):
        """Requirement: an exact cache hit must skip Gemini and complete."""
        cached = {
            "conditions": [{
                "condition_name": "Diabetes",
                "evidence_quote": "Patient has diabetes",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 0.9,
            }],
            "gaps": [],
            "summary": "Patient has diabetes.",
            "model_version": "cached-model",
            "prompt_version": "v1",
        }
        valid_db.get_cached_analysis_result.return_value = cached

        cache_gemini = FakeGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: cache_gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            complete = [data for etype, data in events if etype == "complete"]
            assert len(complete) == 1
            assert complete[0]["analysis"]["summary"] == "Patient has diabetes."
            token_events = [data for etype, data in events if etype == "token"]
            assert token_events == []

        app.dependency_overrides.clear()
        assert cache_gemini.call_count == 0
        valid_db.persist_analysis_for_note.assert_awaited_once()
        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert any(c.kwargs.get("status") == "completed" for c in finish_calls)

    def test_identical_text_different_users_share_cache_key(self, valid_db, app):
        """Requirement: identical note text from different users hits one entry."""
        cached = {
            "conditions": [{
                "condition_name": "Diabetes",
                "evidence_quote": "Patient has diabetes",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 0.9,
            }],
            "gaps": [],
            "summary": "Patient has diabetes.",
            "model_version": "cached-model",
            "prompt_version": "v1",
        }
        valid_db.get_cached_analysis_result.return_value = cached

        user_a = AuthenticatedUser(uid="user-a", email="a@example.com")
        user_b = AuthenticatedUser(uid="user-b", email="b@example.com")
        note_a = make_note(note_id="na", user_id="user-a")
        note_a.raw_text = "same text"
        note_a.analysis_job_id = "job-a"
        note_b = make_note(note_id="nb", user_id="user-b")
        note_b.raw_text = "same text"
        note_b.analysis_job_id = "job-b"

        valid_db.get_note.side_effect = lambda *, note_id, user_id: {
            ("na", "user-a"): note_a,
            ("nb", "user-b"): note_b,
        }[(note_id, user_id)]

        gemini = FakeGemini()
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        app.dependency_overrides[get_current_user] = lambda: user_a
        with TestClient(app) as c:
            text_a = c.get(f"/notes/{note_a.id}/analysis/stream").text
        app.dependency_overrides[get_current_user] = lambda: user_b
        with TestClient(app) as c:
            text_b = c.get(f"/notes/{note_b.id}/analysis/stream").text
        app.dependency_overrides.clear()

        cache_keys = [
            c.args[0] for c in valid_db.get_cached_analysis_result.await_args_list
        ]
        assert cache_keys[0] == cache_keys[1]
        assert "user-a" not in cache_keys[0]
        assert "user-b" not in cache_keys[0]
        for text in (text_a, text_b):
            events = _parse_sse(text)
            assert any(etype == "complete" for etype, _ in events)
        assert gemini.call_count == 0

    def test_partial_output_is_classified_as_invalid_output(self, valid_db, app):
        """Requirement: malformed streamed output is classified as invalid_output."""
        bad_gemini = FakeGemini()
        bad_gemini.call_count = 0
        async def bad_stream(note_text, deadline_at=None, request_contents=None):
            bad_gemini.call_count += 1
            yield "SUMMARY:\n"
            yield "Partial summary without JSON data"
        bad_gemini.stream_analyze_note = bad_stream

        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: bad_gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            error_events = [d for etype, d in events if etype == "error"]
            assert len(error_events) == 1

            finish_calls = valid_db.finish_analysis_job.await_args_list
            assert all(c.kwargs.get("status") == "failed" for c in finish_calls)
            assert any(
                c.kwargs.get("error_reason") == "invalid_output"
                for c in finish_calls
            )

        app.dependency_overrides.clear()
        assert bad_gemini.call_count == 2

    def test_schema_invalid_streamed_output_is_classified_as_invalid_output(self, valid_db, app):
        """Requirement: schema-invalid streamed output is classified as invalid_output."""
        bad_gemini = FakeGemini()
        bad_gemini.call_count = 0
        async def bad_stream(note_text, deadline_at=None, request_contents=None):
            bad_gemini.call_count += 1
            yield '{"conditions": [], "gaps": []}'
        bad_gemini.stream_analyze_note = bad_stream

        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: bad_gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            error_events = [d for etype, d in events if etype == "error"]
            assert len(error_events) == 1

            finish_calls = valid_db.finish_analysis_job.await_args_list
            assert all(c.kwargs.get("status") == "failed" for c in finish_calls)
            assert any(
                c.kwargs.get("error_reason") == "invalid_output"
                for c in finish_calls
            )

        app.dependency_overrides.clear()
        assert bad_gemini.call_count == 2

    def test_similar_cache_hit_skips_gemini_and_emits_complete(self, valid_db, app):
        """Requirement: a safe similar hit (exact miss) skips Gemini on SSE."""
        note_text = "Patient has Type 2 diabetes, controlled on metformin."
        quote = "Type 2 diabetes"
        cond = Condition(
            id="c1",
            condition_name="Type 2 diabetes mellitus",
            evidence_quote=quote,
            documentation_status=DocumentationStatus.WELL_DOCUMENTED,
            suggested_icd10="E11.9",
            confidence=0.9,
            quote_verified=True,
        )
        similar = {
            "shingles": list(build_shingles(tokenize(note_text))),
            "conditions": [cond.model_dump(mode="json", exclude={"id"})],
            "gaps": [],
            "summary": "Patient has diabetes.",
            "model_version": "cached-model",
            "prompt_version": PROMPT_VERSION,
        }
        valid_db.get_cached_analysis_result.return_value = None
        valid_db.find_similar_cached_results = AsyncMock(return_value=[similar])

        gemini = FakeGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)

        complete = [data for etype, data in events if etype == "complete"]
        assert len(complete) == 1
        assert complete[0]["analysis"]["summary"] == "Patient has diabetes."
        assert gemini.call_count == 0


class TestParseStreamedResponse:
    """Focused tests for the post-stream parsing helper."""

    def test_malformed_json_is_classified_as_invalid_output(self):
        with pytest.raises(GeminiAnalysisError) as exc_info:
            _parse_streamed_response(
                raw_text="DATA: {invalid json",
                note_text="Patient has diabetes.",
                settings=get_settings(),
            )
        assert exc_info.value.failure_reason == "invalid_output"

    def test_schema_invalid_json_is_classified_as_invalid_output(self):
        payload = json.dumps({
            "summary": "ok",
            "conditions": [{
                "condition_name": "Diabetes",
                "evidence_quote": "Patient has diabetes",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 5,
            }],
            "gaps": [],
        })
        with pytest.raises(GeminiAnalysisError) as exc_info:
            _parse_streamed_response(
                raw_text=f"DATA: {payload}",
                note_text="Patient has diabetes.",
                settings=get_settings(),
            )
        assert exc_info.value.failure_reason == "invalid_output"

    def test_empty_response_is_classified_as_invalid_output(self):
        with pytest.raises(GeminiAnalysisError) as exc_info:
            _parse_streamed_response(
                raw_text="",
                note_text="Patient has diabetes.",
                settings=get_settings(),
            )
        assert exc_info.value.failure_reason == "invalid_output"

    def test_valid_output_succeeds(self):
        payload = json.dumps({
            "conditions": [{
                "condition_name": "Diabetes",
                "evidence_quote": "Patient has diabetes",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 0.9,
            }],
            "gaps": [],
            "summary": "Patient has diabetes.",
        })
        result = _parse_streamed_response(
            raw_text=f"SUMMARY: test\nDATA: {payload}",
            note_text="Patient has Type 2 diabetes mellitus controlled on metformin.",
            settings=get_settings(),
        )
        assert result.summary == "Patient has diabetes."
        assert len(result.conditions) == 1
        assert result.conditions[0].condition_name == "Diabetes"


class TestStreamRetry:
    """Focused tests for post-stream invalid_output retry behavior."""

    def test_first_invalid_second_valid_succeeds(self, valid_db, app):
        class RetrySuccessGemini:
            def __init__(self):
                self.call_count = 0
                self.request_contents_list = []
                self._prompt_template = "Analyze: {note_text}"

            def stream_analyze_note(self, note_text, deadline_at=None, request_contents=None):
                self.call_count += 1
                self.request_contents_list.append(request_contents)
                async def _stream():
                    if self.call_count == 1:
                        yield '{"conditions": [], "gaps": []}'
                    else:
                        yield VALID_JSON
                return _stream()

        gemini = RetrySuccessGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            complete = [data for etype, data in events if etype == "complete"]
            assert len(complete) == 1

        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert any(c.kwargs.get("status") == "completed" for c in finish_calls)
        assert gemini.call_count == 2
        assert gemini.request_contents_list[0] is None
        assert "CORRECTION REQUIRED" in (gemini.request_contents_list[1] or "")
        app.dependency_overrides.clear()

    def test_both_invalid_fails_with_invalid_output(self, valid_db, app):
        class RetryFailGemini:
            def __init__(self):
                self.call_count = 0
                self.request_contents_list = []
                self._prompt_template = "Analyze: {note_text}"

            def stream_analyze_note(self, note_text, deadline_at=None, request_contents=None):
                self.call_count += 1
                self.request_contents_list.append(request_contents)
                async def _stream():
                    yield '{"conditions": [], "gaps": []}'
                return _stream()

        gemini = RetryFailGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            error_events = [data for etype, data in events if etype == "error"]
            assert len(error_events) == 1

        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert all(c.kwargs.get("status") == "failed" for c in finish_calls)
        assert any(c.kwargs.get("error_reason") == "invalid_output" for c in finish_calls)
        assert gemini.call_count == 2
        assert gemini.request_contents_list[0] is None
        assert "CORRECTION REQUIRED" in (gemini.request_contents_list[1] or "")
        app.dependency_overrides.clear()

    def test_provider_error_does_not_trigger_retry(self, valid_db, app):
        class ProviderErrorGemini:
            def __init__(self):
                self.call_count = 0

            def stream_analyze_note(self, note_text, deadline_at=None, request_contents=None):
                self.call_count += 1
                raise RuntimeError("provider error")

        gemini = ProviderErrorGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            error_events = [data for etype, data in events if etype == "error"]
            assert len(error_events) == 1

        assert gemini.call_count == 1
        app.dependency_overrides.clear()

    def test_valid_first_response_has_one_attempt(self, valid_db, app):
        class ValidFirstGemini:
            def __init__(self):
                self.call_count = 0

            def stream_analyze_note(self, note_text, deadline_at=None, request_contents=None):
                self.call_count += 1
                async def _stream():
                    yield VALID_JSON
                return _stream()

        gemini = ValidFirstGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            complete = [data for etype, data in events if etype == "complete"]
            assert len(complete) == 1

        assert gemini.call_count == 1
        app.dependency_overrides.clear()

    def test_corrupt_exact_cache_falls_back_to_gemini_in_stream(self, valid_db, app):
        """Corrupt cached condition data must not reach the user; the streaming
        path must fall back to Gemini and emit a complete analysis."""
        cached = {
            "conditions": [{
                "condition_name": "Diabetes",
                "evidence_quote": "Patient has diabetes",
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 5,
            }],
            "gaps": [],
            "summary": "Patient has diabetes.",
            "model_version": "cached-model",
            "prompt_version": "v1",
        }
        valid_db.get_cached_analysis_result.return_value = cached

        gemini = FakeGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            complete = [data for etype, data in events if etype == "complete"]
            assert len(complete) == 1
            assert complete[0]["analysis"]["summary"] == "Patient has diabetes."

        app.dependency_overrides.clear()
        assert gemini.call_count == 1
        valid_db.persist_analysis_for_note.assert_awaited_once()
        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert any(c.kwargs.get("status") == "completed" for c in finish_calls)

    def test_similar_cache_lookup_failure_falls_back_to_gemini_in_stream(self, valid_db, app):
        """When the similar-cache Firestore lookup raises, the streaming path
        must fall back to Gemini and complete successfully."""
        valid_db.get_cached_analysis_result.return_value = None
        valid_db.find_similar_cached_results.side_effect = RuntimeError(
            "firestore unavailable"
        )

        gemini = FakeGemini()
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: valid_db

        with TestClient(app) as c:
            response = c.get("/notes/n1/analysis/stream")
            events = _parse_sse(response.text)
            complete = [data for etype, data in events if etype == "complete"]
            assert len(complete) == 1
            assert complete[0]["analysis"]["summary"] == "Patient has diabetes."

        app.dependency_overrides.clear()
        assert gemini.call_count == 1
        valid_db.persist_analysis_for_note.assert_awaited_once()
        finish_calls = valid_db.finish_analysis_job.await_args_list
        assert any(c.kwargs.get("status") == "completed" for c in finish_calls)
