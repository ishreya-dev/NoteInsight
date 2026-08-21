"""Firestore persistence for notes, analyses, and reviews."""

from __future__ import annotations

from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

from app.config import Settings, get_settings
from app.models.analysis import Analysis, Review, ReviewCreate
from app.models.note import Note, NoteCreate, NoteListItem, ReviewStatus
from app.services.firestore_codec import (
    DocumentConflictError,
    DocumentDataError,
    clamp_history_limit,
    dump_document,
    is_already_exists,
    parse_model,
    review_document_id,
    should_mark_note_reviewed_for_analysis,
    validate_condition_count,
)


_NOTES_COLLECTION = "notes"
_ANALYSES_COLLECTION = "analyses"
_REVIEWS_COLLECTION = "reviews"
_ANALYSIS_JOBS_COLLECTION = "analysis_jobs"


class FirestoreClient:
    """User-scoped asynchronous Firestore access."""

    def __init__(self, settings: Settings) -> None:
        if settings.firebase_service_account_path:
            credentials = service_account.Credentials.from_service_account_file(
                settings.firebase_service_account_path
            )
            self._db = AsyncClient(
                project=settings.firebase_project_id,
                credentials=credentials,
            )
        else:
            self._db = AsyncClient(project=settings.firebase_project_id)

    async def create_note(
        self,
        user_id: str,
        note_id: str,
        payload: NoteCreate,
    ) -> Note:
        note = Note(
            id=note_id,
            user_id=user_id,
            raw_text=payload.raw_text,
            pseudonym=payload.pseudonym,
            visit_date=payload.visit_date,
        )
        document = self._db.collection(_NOTES_COLLECTION).document(note_id)
        try:
            await document.create(dump_document(note, exclude={"id"}))
        except Exception as exc:
            if is_already_exists(exc):
                raise DocumentConflictError(
                    f"Note '{note_id}' already exists"
                ) from exc
            raise
        return note

    async def list_notes(
        self,
        user_id: str,
        limit: int = 50,
    ) -> list[NoteListItem]:
        limit = clamp_history_limit(limit)
        query = (
            self._db.collection(_NOTES_COLLECTION)
            .where(filter=FieldFilter("user_id", "==", user_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        items: list[NoteListItem] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            try:
                items.append(
                    NoteListItem(
                        id=snapshot.id,
                        pseudonym=data.get("pseudonym"),
                        visit_date=data.get("visit_date"),
                        created_at=data["created_at"],
                        review_status=data.get(
                            "review_status", ReviewStatus.PENDING
                        ),
                        condition_count=data.get("condition_count", 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DocumentDataError(
                    f"Invalid Note document '{snapshot.id}'"
                ) from exc
        return items

    async def get_note(
        self,
        note_id: str,
        user_id: str,
    ) -> Note | None:
        snapshot = await (
            self._db.collection(_NOTES_COLLECTION).document(note_id).get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("user_id") != user_id:
            return None
        return parse_model(Note, snapshot.id, data)

    async def create_analysis_job(
        self,
        *,
        job_id: str,
        note_id: str,
        user_id: str,
    ) -> None:
        job_ref = self._db.collection(_ANALYSIS_JOBS_COLLECTION).document(job_id)
        note_ref = self._db.collection(_NOTES_COLLECTION).document(note_id)
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def create(transaction: Any) -> None:
            note_snapshot = await note_ref.get(transaction=transaction)
            if not note_snapshot.exists:
                raise LookupError("Note not found")
            note_data = note_snapshot.to_dict() or {}
            if note_data.get("user_id") != user_id:
                raise PermissionError("Note belongs to a different user")
            job_snapshot = await job_ref.get(transaction=transaction)
            if job_snapshot.exists:
                return
            transaction.create(
                job_ref,
                {
                    "note_id": note_id,
                    "user_id": user_id,
                    "status": "pending",
                },
            )
            transaction.update(note_ref, {"analysis_job_id": job_id})

        await create(transaction)

    async def claim_analysis_job(self, *, job_id: str, user_id: str) -> str | None:
        """Claim a pending job or return its current status."""
        job_ref = self._db.collection(_ANALYSIS_JOBS_COLLECTION).document(job_id)
        transaction = self._db.transaction()
        claimed_status: str | None = None

        @firestore.async_transactional
        async def claim(transaction: Any) -> None:
            nonlocal claimed_status
            snapshot = await job_ref.get(transaction=transaction)
            if not snapshot.exists:
                return
            data = snapshot.to_dict() or {}
            if data.get("user_id") != user_id:
                return
            claimed_status = data.get("status")
            if claimed_status == "pending":
                transaction.update(job_ref, {"status": "processing"})
                claimed_status = "claimed"

        await claim(transaction)
        return claimed_status

    async def get_analysis_job(
        self, *, job_id: str, user_id: str
    ) -> dict[str, Any] | None:
        snapshot = await (
            self._db.collection(_ANALYSIS_JOBS_COLLECTION).document(job_id).get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("user_id") != user_id:
            return None
        return data

    async def finish_analysis_job(
        self,
        *,
        job_id: str,
        status: str,
        analysis_id: str | None = None,
        error_message: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"status": status}
        if analysis_id is not None:
            data["analysis_id"] = analysis_id
        if error_message is not None:
            data["error_message"] = error_message
        if error_reason is not None:
            data["error_reason"] = error_reason
        await (
            self._db.collection(_ANALYSIS_JOBS_COLLECTION)
            .document(job_id)
            .update(data)
        )

    async def persist_analysis_for_note(
        self,
        analysis: Analysis,
        *,
        condition_count: int,
    ) -> None:
        validate_condition_count(condition_count)
        analysis_ref = self._db.collection(_ANALYSES_COLLECTION).document(
            analysis.id
        )
        note_ref = self._db.collection(_NOTES_COLLECTION).document(
            analysis.note_id
        )
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def persist(transaction: Any) -> None:
            note_snapshot = await note_ref.get(transaction=transaction)
            analysis_snapshot = await analysis_ref.get(transaction=transaction)
            if not note_snapshot.exists:
                raise LookupError("Note not found")
            note_data = note_snapshot.to_dict() or {}
            if note_data.get("user_id") != analysis.user_id:
                raise PermissionError("Note belongs to a different user")
            if analysis_snapshot.exists:
                raise DocumentConflictError(
                    f"Analysis '{analysis.id}' already exists"
                )
            transaction.create(
                analysis_ref,
                dump_document(analysis, exclude={"id"}),
            )
            transaction.update(
                note_ref,
                {
                    "latest_analysis_id": analysis.id,
                    "review_status": ReviewStatus.PENDING.value,
                    "condition_count": condition_count,
                },
            )

        await persist(transaction)

    async def get_analysis(
        self,
        analysis_id: str,
        user_id: str,
    ) -> Analysis | None:
        snapshot = await (
            self._db.collection(_ANALYSES_COLLECTION)
            .document(analysis_id)
            .get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("user_id") != user_id:
            return None
        return parse_model(Analysis, snapshot.id, data)

    async def get_review_for_analysis(
        self,
        analysis_id: str,
        user_id: str,
    ) -> Review | None:
        analysis = await self.get_analysis(analysis_id, user_id)
        if analysis is None:
            return None
        snapshot = await (
            self._db.collection(_REVIEWS_COLLECTION)
            .document(review_document_id(analysis_id))
            .get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("user_id") != user_id:
            return None
        return parse_model(Review, snapshot.id, data)

    async def upsert_review(
        self,
        *,
        analysis_id: str,
        note_id: str,
        user_id: str,
        payload: ReviewCreate,
        condition_count: int,
    ) -> tuple[Review, bool]:
        validate_condition_count(condition_count)
        review_id = review_document_id(analysis_id)
        review_ref = self._db.collection(_REVIEWS_COLLECTION).document(review_id)
        note_ref = self._db.collection(_NOTES_COLLECTION).document(note_id)
        existing = await review_ref.get()
        created = not existing.exists
        review_data = {
            "id": review_id,
            "analysis_id": analysis_id,
            "note_id": note_id,
            "user_id": user_id,
            "conditions": list(payload.conditions),
            "gaps": list(payload.gaps),
            "reviewer_notes": payload.notes,
        }
        if not created:
            existing_data = existing.to_dict() or {}
            if "created_at" in existing_data:
                review_data["created_at"] = existing_data["created_at"]
        review = Review.model_validate(review_data)
        data = dump_document(review, exclude={"id"})
        await review_ref.set(data, merge=True)

        note_snapshot = await note_ref.get()
        note_data = note_snapshot.to_dict() or {}
        if should_mark_note_reviewed_for_analysis(note_data, analysis_id):
            await note_ref.update(
                {
                    "review_status": ReviewStatus.REVIEWED.value,
                    "condition_count": condition_count,
                }
            )
        return review, created


def get_firestore_client() -> FirestoreClient:
    return FirestoreClient(get_settings())
