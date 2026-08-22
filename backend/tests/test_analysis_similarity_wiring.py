"""Wiring tests for the exact -> similar -> Gemini lookup order.

Exercises the real analysis flow in ``_analyze_and_persist`` and asserts that
the similar-cache lookup is only consulted after an exact miss, and that
Gemini is skipped whenever a safe cached result (exact or similar) is found.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import get_settings
from app.models.analysis import Condition, DocumentationStatus
from app.services.analysis_jobs import _analyze_and_persist
from app.services.gemini_client import AnalysisResult, PROMPT_VERSION
from app.services.similarity import build_shingles, tokenize
from tests.conftest import make_note

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
    db = MagicMock()
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
    gemini.analyze_note = AsyncMock(
        return_value=AnalysisResult(
            conditions=[
                Condition(
                    id="c2",
                    condition_name="Type 2 diabetes mellitus",
                    evidence_quote=QUOTE,
                    documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                    suggested_icd10="E11.9",
                    confidence=0.9,
                    quote_verified=True,
                )
            ],
            gaps=[],
            summary="gemini summary",
            model_version="gemini-test",
            prompt_version=PROMPT_VERSION,
        )
    )
    return gemini


@pytest.mark.asyncio
async def test_exact_hit_skips_similar_lookup_and_gemini():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=_cached_payload())
    gemini = _gemini()

    analysis = await _analyze_and_persist(note, db, gemini, get_settings())

    gemini.analyze_note.assert_not_called()
    db.find_similar_cached_results.assert_not_called()
    db.cache_analysis_result.assert_not_called()
    assert analysis.summary == "Patient has diabetes."


@pytest.mark.asyncio
async def test_exact_miss_safe_similar_hit_skips_gemini():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[_cached_payload()])
    gemini = _gemini()

    analysis = await _analyze_and_persist(note, db, gemini, get_settings())

    db.find_similar_cached_results.assert_awaited_once()
    gemini.analyze_note.assert_not_called()
    db.cache_analysis_result.assert_not_called()
    assert analysis.summary == "Patient has diabetes."


@pytest.mark.asyncio
async def test_exact_miss_no_safe_similar_calls_gemini():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[])
    gemini = _gemini()

    analysis = await _analyze_and_persist(note, db, gemini, get_settings())

    db.find_similar_cached_results.assert_awaited_once()
    gemini.analyze_note.assert_awaited_once()
    db.cache_analysis_result.assert_awaited_once()
    assert analysis.summary == "gemini summary"


@pytest.mark.asyncio
async def test_full_cache_miss_keeps_existing_gemini_behavior():
    note = make_note(note_id="n1")
    note.raw_text = BASE
    db = _db(get_cached=None, similar=[])
    gemini = _gemini()

    analysis = await _analyze_and_persist(note, db, gemini, get_settings())

    gemini.analyze_note.assert_awaited_once()
    db.cache_analysis_result.assert_awaited_once()
    db.persist_analysis_for_note.assert_awaited_once()
    assert analysis.summary == "gemini summary"
    assert not analysis.is_failed
