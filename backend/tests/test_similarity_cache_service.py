"""Focused tests for the similarity-cache service-layer orchestration."""

import pytest

from app.services.analysis_jobs import find_similar_cached_analysis
from app.services.similarity import build_shingles, tokenize

# Long clinical note so a single-word change still exceeds the 0.95 threshold.
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


def _candidate(note_text=BASE, quote=QUOTE, summary="s"):
    return {
        "shingles": list(build_shingles(tokenize(note_text))),
        "conditions": [
            {"condition_name": "Type 2 diabetes mellitus", "evidence_quote": quote}
        ],
        "summary": summary,
    }


class _FakeDB:
    def __init__(self, candidates):
        self.candidates = candidates
        self.find_similar_cached_results_calls = 0

    async def find_similar_cached_results(self, buckets, limit=10):
        self.find_similar_cached_results_calls += 1
        return self.candidates


@pytest.mark.asyncio
async def test_safe_candidate_returned():
    note_text = BASE.replace("reports good adherence", "reports excellent adherence")
    candidate = _candidate(BASE, summary="base")
    db = _FakeDB([candidate])

    result = await find_similar_cached_analysis(db, note_text)

    assert result is candidate
    assert db.find_similar_cached_results_calls == 1


@pytest.mark.asyncio
async def test_no_candidates_returns_none():
    db = _FakeDB([])
    result = await find_similar_cached_analysis(db, BASE)
    assert result is None


@pytest.mark.asyncio
async def test_candidates_found_but_none_safe_returns_none():
    note_text = BASE.replace("lisinopril", "insulin")
    candidate = _candidate(BASE, summary="base")
    db = _FakeDB([candidate])

    result = await find_similar_cached_analysis(db, note_text)

    assert result is None


@pytest.mark.asyncio
async def test_firestore_exception_returns_none_and_is_logged(caplog):
    class _FailingDB:
        async def find_similar_cached_results(self, buckets, limit=10):
            raise RuntimeError("firestore unavailable")

    caplog.set_level("WARNING")
    result = await find_similar_cached_analysis(_FailingDB(), BASE)

    assert result is None
    assert any(
        "Failed to read similar analysis cache" in record.getMessage()
        for record in caplog.records
    )
