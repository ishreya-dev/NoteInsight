"""Wiring tests for the exact -> similar -> Gemini lookup order.

Asserts that the similar-cache lookup is only consulted after an exact miss,
and that Gemini is skipped whenever a safe cached result (exact or similar)
is found.
"""

from unittest.mock import AsyncMock, MagicMock

import json
import pytest

from app.config import get_settings
from app.models.analysis import Condition, DocumentationStatus
from app.services.analysis_jobs import _parse_cached_result, stream_analysis_and_persist
from app.services.gemini_client import PROMPT_VERSION
from app.services.similarity import build_shingles, tokenize
from tests.conftest import TEST_USER, make_note

BASE = (
    "patient is a 64 year old male with type 2 diabetes mellitus hypertension "
    "and hyperlipidemia managed on metformin lisinopril and atorvastatin with "
    "adequate glycemic control and stable vitals no known drug allergies "
    "followed in clinic for routine care reports good adherence to medications "
    "and no tobacco history with regular exercise and balanced diet sleeps "
    "well and has no family history of cardiac disease ambulates without "
    "assistance and maintains healthy weight and attends annual wellness visits "
    "and denies chest pain and has normal renal function and normal liver "
    "function and no known malignancy and is up to date on vaccinations and is "
    "independent with activities of daily living and has stable body mass index "
    "and no history of stroke and no history of seizures and takes aspirin "
    "daily and monitors blood glucose weekly"
)
QUOTE = "type 2 diabetes mellitus"


def _make_condition(quote=QUOTE):
    cond = Condition(
        id="c1",
        condition_name="Type 2 diabetes mellitus",
        evidence_quote=quote,
        documentation_status=DocumentationStatus.WELL_DOCUMENTED,
        suggested_icd10="E11.9",
        confidence=0.9,
        quote_verified=True,
    )
    return cond.model_dump(mode="json", exclude={"id"})


def _cached_payload(text=BASE, quote=QUOTE, summary="Patient has diabetes."):
    return {
        "shingles": list(build_shingles(tokenize(text))),
        "conditions": [_make_condition(quote)],
        "gaps": [],
        "summary": summary,
        "model_version": "gemini-test",
        "prompt_version": PROMPT_VERSION,
    }


def _db(get_cached=None, similar=None):
    db = AsyncMock()
    db.get_note = AsyncMock()
    db.get_analysis_job = AsyncMock()
    db.claim_analysis_job = AsyncMock(return_value="claimed")
    db.get_cached_analysis_result = AsyncMock(return_value=get_cached)
    db.find_similar_cached_results = AsyncMock(
        return_value=similar if similar is not None else []
    )
    db.persist_analysis_for_note = AsyncMock()
    db.finish_analysis_job = AsyncMock()
    db.cache_analysis_result = AsyncMock()
    return db


def _gemini():
    gemini = MagicMock()
    payload = {
        "conditions": [
            {
                "condition_name": "Type 2 diabetes mellitus",
                "evidence_quote": QUOTE,
                "documentation_status": "well_documented",
                "suggested_icd10": "E11.9",
                "confidence": 0.9,
                "quote_verified": True,
            }
        ],
        "gaps": [],
        "summary": "gemini summary",
    }
    raw = "SUMMARY: gemini summary\nDATA:" + json.dumps(payload)

    async def _stream(*_a, **_kw):
        for chunk in (raw[: len(raw) // 2], raw[len(raw) // 2 :]):
            yield chunk

    gemini.stream_analyze_note = _stream
    return gemini


@pytest.mark.asyncio
async def test_exact_hit_skips_similar_lookup_and_gemini():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=_cached_payload())
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    analysis = await stream_analysis_and_persist(
        note, db, gemini, get_settings()
    )

    assert stream_calls == 0
    db.find_similar_cached_results.assert_not_called()
    db.cache_analysis_result.assert_not_called()
    assert analysis.summary == "Patient has diabetes."
    assert analysis.is_failed is False


@pytest.mark.asyncio
async def test_exact_miss_safe_similar_hit_skips_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.routers import notes as notes_router
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[_cached_payload()])
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    original_find_similar = notes_router.find_similar_cached_analysis
    notes_router.find_similar_cached_analysis = AsyncMock(return_value=_cached_payload())
    try:
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
        app.dependency_overrides[get_gemini_client] = lambda: gemini
        app.dependency_overrides[get_firestore_client] = lambda: db

        with TestClient(app) as client:
            response = client.get(f"/notes/{note.id}/analysis/stream")
            assert response.status_code == 200
            assert "event: complete" in response.text
            assert "Patient has diabetes." in response.text
            assert "event: token" not in response.text

        app.dependency_overrides.clear()
        assert stream_calls == 0
    finally:
        notes_router.find_similar_cached_analysis = original_find_similar


@pytest.mark.asyncio
async def test_exact_miss_no_safe_similar_calls_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[])
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1


@pytest.mark.asyncio
async def test_full_cache_miss_keeps_existing_gemini_behavior():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[])
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1
    db.cache_analysis_result.assert_awaited_once()
    db.persist_analysis_for_note.assert_awaited_once()


@pytest.mark.asyncio
async def test_cached_valid_quote_is_reverified_as_true():
    analysis = _parse_cached_result(_cached_payload(), BASE)
    assert analysis.conditions[0].quote_verified is True


@pytest.mark.asyncio
async def test_cached_invalid_quote_is_reverified_as_false():
    cached = _cached_payload()
    cached["conditions"][0]["evidence_quote"] = "not in note"
    analysis = _parse_cached_result(cached, BASE)
    assert analysis.conditions[0].quote_verified is False


@pytest.mark.asyncio
async def test_cached_stored_false_but_valid_quote_is_freshly_verified():
    cached = _cached_payload()
    cached["conditions"][0]["quote_verified"] = False
    analysis = _parse_cached_result(cached, BASE)
    assert analysis.conditions[0].quote_verified is True


@pytest.mark.asyncio
async def test_corrupt_cached_confidence_falls_back_to_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    cached = _cached_payload()
    cached["conditions"][0]["confidence"] = 5
    db = _db(get_cached=cached)
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1
    db.cache_analysis_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_corrupt_cached_enum_falls_back_to_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    cached = _cached_payload()
    cached["conditions"][0]["documentation_status"] = "not_a_status"
    db = _db(get_cached=cached)
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1
    db.cache_analysis_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_corrupt_cached_gap_field_falls_back_to_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    cached = _cached_payload()
    cached["gaps"] = [{"related_condition": "Type 2 diabetes mellitus"}]
    db = _db(get_cached=cached)
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1
    db.cache_analysis_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_exact_cache_hit_skips_gemini():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=_cached_payload())
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    analysis = await stream_analysis_and_persist(
        note, db, gemini, get_settings()
    )

    assert stream_calls == 0
    db.cache_analysis_result.assert_not_called()
    assert analysis.summary == "Patient has diabetes."
    assert analysis.is_failed is False


@pytest.mark.asyncio
async def test_similar_cache_lookup_failure_falls_back_to_gemini():
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user
    from app.main import app
    from app.services.firestore_client import get_firestore_client
    from app.services.gemini_client import get_gemini_client

    note = make_note(note_id="n1").model_copy(update={"analysis_job_id": "job-1"})
    note.raw_text = BASE
    db = _db(get_cached=None)
    db.get_note.return_value = note
    db.get_analysis_job.return_value = {"status": "pending", "user_id": note.user_id}
    db.find_similar_cached_results.side_effect = RuntimeError("firestore unavailable")
    gemini = _gemini()
    stream_calls = 0
    original_stream = gemini.stream_analyze_note

    async def counting_stream(*_a, **_kw):
        nonlocal stream_calls
        stream_calls += 1
        async for chunk in original_stream(*_a, **_kw):
            yield chunk

    gemini.stream_analyze_note = counting_stream

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_gemini_client] = lambda: gemini
    app.dependency_overrides[get_firestore_client] = lambda: db

    with TestClient(app) as client:
        response = client.get(f"/notes/{note.id}/analysis/stream")
        assert response.status_code == 200
        assert "event: complete" in response.text
        assert "gemini summary" in response.text

    app.dependency_overrides.clear()
    assert stream_calls == 1
