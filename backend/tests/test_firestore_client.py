"""Firestore serialization, parsing, ownership, and consistency helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore
from pydantic import ValidationError

from app.models.analysis import (
    Analysis,
    Condition,
    DocumentationGap,
    DocumentationStatus,
)
from app.models.note import Note, ReviewStatus
from app.services import firestore_client
from app.services.firestore_client import FirestoreClient
from app.services.firestore_codec import (
    DocumentDataError,
    clamp_history_limit,
    dump_document,
    is_already_exists,
    parse_model,
    review_document_id,
    should_mark_note_reviewed_for_analysis,
    to_firestore,
)


def _firestore_client_with_snapshot(snapshot: MagicMock) -> FirestoreClient:
    client = FirestoreClient.__new__(FirestoreClient)
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)
    client._db = MagicMock()
    client._db.collection.return_value.document.return_value = doc_ref
    return client


def _sample_analysis(*, failed: bool = False) -> Analysis:
    if failed:
        return Analysis(
            id="a1",
            note_id="n1",
            user_id="u1",
            conditions=(),
            gaps=(),
            summary="Analysis could not be completed. Please try again.",
            model_version="gemini-test",
            prompt_version="v1",
            is_failed=True,
            failure_reason="provider failure",
        )

    return Analysis(
        id="a1",
        note_id="n1",
        user_id="u1",
        conditions=(
            Condition(
                id="c1",
                condition_name="Type 2 diabetes mellitus",
                evidence_quote="Type 2 diabetes, controlled",
                documentation_status=DocumentationStatus.WELL_DOCUMENTED,
                suggested_icd10="E11.9",
                confidence=0.91,
                quote_verified=True,
            ),
        ),
        gaps=(
            DocumentationGap(
                description="Control status not quantified",
                related_condition="Type 2 diabetes mellitus",
            ),
        ),
        summary="Follow-up for diabetes",
        model_version="gemini-test",
        prompt_version="v1",
        created_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )


def test_dump_document_converts_enums_tuples_and_dates() -> None:
    payload = dump_document(_sample_analysis(), exclude={"id"})

    assert isinstance(payload["conditions"], list)
    assert payload["conditions"][0]["documentation_status"] == "well_documented"
    assert isinstance(payload["created_at"], datetime)
    assert payload["created_at"].tzinfo is not None

    note_payload = to_firestore(
        {
            "visit_date": date(2024, 5, 1),
            "review_status": ReviewStatus.PENDING,
            "tags": ("a", "b"),
        }
    )
    assert note_payload["visit_date"] == "2024-05-01"
    assert note_payload["review_status"] == "pending"
    assert note_payload["tags"] == ["a", "b"]


def test_analysis_round_trip_preserves_immutable_shape() -> None:
    analysis = _sample_analysis()
    restored = parse_model(
        Analysis,
        analysis.id,
        dump_document(analysis, exclude={"id"}),
    )

    assert isinstance(restored.conditions, tuple)
    assert isinstance(restored.gaps, tuple)
    assert restored.conditions[0].condition_name == "Type 2 diabetes mellitus"
    with pytest.raises(ValidationError):
        restored.conditions[0].condition_name = "changed"


def test_failed_analysis_round_trip() -> None:
    restored = parse_model(
        Analysis,
        "a1",
        dump_document(_sample_analysis(failed=True), exclude={"id"}),
    )
    assert restored.is_failed is True
    assert restored.conditions == ()


def test_parse_model_rejects_malformed_note() -> None:
    with pytest.raises(DocumentDataError):
        parse_model(
            Note,
            "n1",
            {
                "user_id": "u1",
                "created_at": datetime.now(timezone.utc),
            },
        )


def test_review_document_id_is_deterministic() -> None:
    assert review_document_id("abc") == "abc"
    assert review_document_id("  abc  ") == "abc"
    with pytest.raises(ValueError):
        review_document_id("   ")


def test_note_marked_reviewed_only_for_latest_analysis() -> None:
    assert should_mark_note_reviewed_for_analysis(
        {"latest_analysis_id": "a2"},
        "a2",
    )
    assert not should_mark_note_reviewed_for_analysis(
        {"latest_analysis_id": "a2"},
        "a1",
    )


def test_clamp_history_limit() -> None:
    assert clamp_history_limit(1) == 1
    assert clamp_history_limit(500) == 100
    with pytest.raises(TypeError):
        clamp_history_limit(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        clamp_history_limit(0)


def test_already_exists_detection() -> None:
    class AlreadyExists(Exception):
        pass

    assert is_already_exists(AlreadyExists("doc already exists"))
    assert not is_already_exists(RuntimeError("network down"))


@pytest.mark.asyncio
async def test_get_note_returns_none_for_other_user() -> None:
    owner_id = "user-a"
    other_id = "user-b"
    note_data = {
        "user_id": owner_id,
        "raw_text": "Patient has Type 2 diabetes, controlled on metformin.",
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "review_status": ReviewStatus.PENDING.value,
        "condition_count": 0,
    }
    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.id = "note-a"
    snapshot.to_dict.return_value = note_data
    client = _firestore_client_with_snapshot(snapshot)

    assert await client.get_note("note-a", other_id) is None

    owned = await client.get_note("note-a", owner_id)
    assert owned is not None
    assert owned.id == "note-a"
    assert owned.user_id == owner_id


@pytest.mark.asyncio
async def test_claim_analysis_job_distinguishes_claim_winner_from_existing_processing() -> None:
    pending_snapshot = MagicMock()
    pending_snapshot.exists = True
    pending_snapshot.to_dict.return_value = {
        "user_id": "user-a",
        "status": "pending",
    }
    processing_snapshot = MagicMock()
    processing_snapshot.exists = True
    processing_snapshot.to_dict.return_value = {
        "user_id": "user-a",
        "status": "processing",
    }

    job_ref = MagicMock()
    job_ref.get = AsyncMock(side_effect=[pending_snapshot, processing_snapshot])
    transaction = MagicMock()
    client = FirestoreClient.__new__(FirestoreClient)
    client._db = MagicMock()
    client._db.collection.return_value.document.return_value = job_ref
    client._db.transaction.return_value = transaction

    first_result = await client.claim_analysis_job(
        job_id="job-1",
        user_id="user-a",
    )
    second_result = await client.claim_analysis_job(
        job_id="job-1",
        user_id="user-a",
    )

    assert first_result == "claimed"
    assert second_result == "processing"
    transaction.update.assert_called_once()


@pytest.mark.asyncio
async def test_list_notes_filters_orders_and_limits_query(monkeypatch) -> None:
    snapshot = MagicMock()
    snapshot.id = "note-a"
    snapshot.to_dict.return_value = {
        "pseudonym": "Pt-A",
        "visit_date": None,
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "review_status": ReviewStatus.PENDING.value,
        "condition_count": 0,
    }

    async def stream():
        yield snapshot

    query = MagicMock()
    query.where.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.stream.return_value = stream()

    client = FirestoreClient.__new__(FirestoreClient)
    client._db = MagicMock()
    client._db.collection.return_value = query
    monkeypatch.setattr(
        firestore_client,
        "FieldFilter",
        lambda field_path, operator, value: (field_path, operator, value),
    )

    items = await client.list_notes(user_id="user-a", limit=7)

    assert [item.id for item in items] == ["note-a"]
    client._db.collection.assert_called_once_with("notes")
    query.where.assert_called_once()
    assert query.where.call_args.kwargs["filter"] == (
        "user_id",
        "==",
        "user-a",
    )
    query.order_by.assert_called_once_with(
        "created_at", direction=firestore.Query.DESCENDING
    )
    query.limit.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_list_notes_propagates_missing_index_error() -> None:
    query = MagicMock()
    query.where.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.stream.side_effect = FailedPrecondition("index is required")

    client = FirestoreClient.__new__(FirestoreClient)
    client._db = MagicMock()
    client._db.collection.return_value = query

    with pytest.raises(FailedPrecondition, match="index is required"):
        await client.list_notes(user_id="user-a")


@pytest.mark.asyncio
async def test_get_analysis_returns_none_for_other_user() -> None:
    owner_id = "user-a"
    other_id = "user-b"
    analysis = _sample_analysis()
    analysis_data = dump_document(analysis, exclude={"id"})
    analysis_data["user_id"] = owner_id
    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.id = analysis.id
    snapshot.to_dict.return_value = analysis_data
    client = _firestore_client_with_snapshot(snapshot)

    assert await client.get_analysis(analysis.id, other_id) is None

    owned = await client.get_analysis(analysis.id, owner_id)
    assert owned is not None
    assert owned.id == analysis.id
    assert owned.user_id == owner_id
