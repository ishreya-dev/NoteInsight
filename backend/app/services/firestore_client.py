"""Firestore persistence for notes, analyses, and reviews."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

from app.config import Settings, get_settings
from app.models.analysis import Analysis, AnalysisJobStatus, Review, ReviewCreate
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
_ANALYSIS_CACHE_COLLECTION = "analysis_cache"
_RATE_LIMITS_COLLECTION = "rate_limits"

# Maximum number of candidate cache entries returned by a similarity lookup.
_SIMILAR_CANDIDATE_LIMIT = 10
# Firestore `array_contains_any` accepts at most this many values per query.
_MAX_CONTAINS_ANY = 30
# Global analysis cache entries expire this many days after creation. The
# actual deletion is handled by a Firestore TTL policy on the `expires_at`
# field; this module only stamps that timestamp when writing entries.
_ANALYSIS_CACHE_TTL_DAYS = 15


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
                    "status": AnalysisJobStatus.PENDING.value,
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
            if claimed_status == AnalysisJobStatus.PENDING.value:
                transaction.update(
                    job_ref, {"status": AnalysisJobStatus.PROCESSING.value}
                )
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

    async def list_reviews_for_user(
        self,
        user_id: str,
        limit: int = 200,
    ) -> list[Review]:
        """Return the user's reviews, most recent first, for metrics use."""
        limit = clamp_history_limit(limit)
        query = (
            self._db.collection(_REVIEWS_COLLECTION)
            .where(filter=FieldFilter("user_id", "==", user_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        reviews: list[Review] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            reviews.append(parse_model(Review, snapshot.id, data))
        return reviews

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
        transaction = self._db.transaction()
        result: dict[str, Any] = {}

        @firestore.async_transactional
        async def upsert(transaction: Any) -> None:
            existing = await review_ref.get(transaction=transaction)
            note_snapshot = await note_ref.get(transaction=transaction)

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
            transaction.set(review_ref, data, merge=True)

            note_data = note_snapshot.to_dict() or {}
            if should_mark_note_reviewed_for_analysis(note_data, analysis_id):
                transaction.update(
                    note_ref,
                    {
                        "review_status": ReviewStatus.REVIEWED.value,
                        "condition_count": condition_count,
                    },
                )

            result["review"] = review
            result["created"] = created

        await upsert(transaction)
        return result["review"], result["created"]

    async def get_cached_analysis_result(
        self,
        note_text_hash: str,
    ) -> dict[str, Any] | None:
        """Return a cached Gemini result payload for identical note text.

        The cache is global (not scoped to a user) because it stores only
        the model's structured output for a given exact note text, never a
        user id, note id, or any other per-user data.
        """
        snapshot = await (
            self._db.collection(_ANALYSIS_CACHE_COLLECTION)
            .document(note_text_hash)
            .get()
        )
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or None

    async def cache_analysis_result(
        self,
        note_text_hash: str,
        result_payload: dict[str, Any],
        buckets: list[str] | None = None,
        signature: list[int] | None = None,
        shingles: list[str] | None = None,
    ) -> None:
        """Best-effort write of a Gemini result payload to the shared cache.

        Uses create (not set) so a concurrent write for the same content
        never overwrites another; the first successful result wins and
        later callers simply reuse it. A create conflict is expected and
        ignored, not an error.

        When provided, ``buckets`` (LSH band ids) and ``signature`` (the
        MinHash signature) are stored alongside the result so the entry can
        later be returned as a near-duplicate candidate. ``shingles`` stores
        the cached note's word-shingle set so a future lookup can compute an
        accurate lexical (Jaccard) similarity rather than relying on the
        approximate MinHash signature. The cache remains global: no user id
        or note id is ever stored here.
        """
        payload = dict(result_payload)
        if buckets is not None:
            payload["buckets"] = list(buckets)
        if signature is not None:
            payload["signature"] = list(signature)
        if shingles is not None:
            payload["shingles"] = list(shingles)
        payload["expires_at"] = datetime.now(timezone.utc) + timedelta(
            days=_ANALYSIS_CACHE_TTL_DAYS
        )

        cache_ref = self._db.collection(_ANALYSIS_CACHE_COLLECTION).document(
            note_text_hash
        )
        try:
            await cache_ref.create(payload)
        except Exception as exc:
            if not is_already_exists(exc):
                raise

    async def find_similar_cached_results(
        self,
        buckets: list[str],
        limit: int = _SIMILAR_CANDIDATE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return a bounded set of near-duplicate candidate cache entries.

        Looks up the global ``analysis_cache`` collection for entries whose
        stored LSH ``buckets`` intersect the supplied candidate ``buckets``.
        This is only a coarse candidate filter; the caller is responsible for
        exact re-scoring (e.g. Jaccard over signatures) and evidence
        validation. No user id or note id is used, so the cache stays global.

        The result is capped at ``limit`` entries and de-duplicated by
        document id when the bucket list must be split across multiple
        ``array_contains_any`` queries (Firestore allows at most
        ``_MAX_CONTAINS_ANY`` values per query).
        """
        bucket_list = list(buckets)
        if not bucket_list:
            return []

        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(bucket_list), _MAX_CONTAINS_ANY):
            chunk = bucket_list[start : start + _MAX_CONTAINS_ANY]
            snapshot = await (
                self._db.collection(_ANALYSIS_CACHE_COLLECTION)
                .where("buckets", "array_contains_any", chunk)
                .limit(limit)
                .get()
            )
            for doc in snapshot:
                if doc.id not in results:
                    results[doc.id] = doc.to_dict() or {}
                if len(results) >= limit:
                    return list(results.values())
        return list(results.values())

    async def consume_rate_limit_slot(
        self,
        *,
        user_id: str,
        max_requests: int,
        window_seconds: int,
    ) -> bool:
        """Atomically consume one request slot in the user's current window.

        Uses a Firestore transaction on a per-user, per-window document so
        the limit is enforced correctly across multiple backend instances,
        not just within a single process. Returns True if the request is
        allowed (and is counted), False if the user is over the limit for
        the current window.
        """
        window_start = (int(time.time()) // window_seconds) * window_seconds
        doc_id = f"{user_id}:{window_start}"
        limit_ref = self._db.collection(_RATE_LIMITS_COLLECTION).document(doc_id)
        transaction = self._db.transaction()
        allowed = False

        @firestore.async_transactional
        async def consume(transaction: Any) -> None:
            nonlocal allowed
            snapshot = await limit_ref.get(transaction=transaction)
            if not snapshot.exists:
                transaction.create(
                    limit_ref,
                    {
                        "user_id": user_id,
                        "window_start": window_start,
                        "count": 1,
                    },
                )
                allowed = True
                return

            data = snapshot.to_dict() or {}
            count = data.get("count", 0)
            if count >= max_requests:
                allowed = False
                return

            transaction.update(limit_ref, {"count": count + 1})
            allowed = True

        await consume(transaction)
        return allowed


def get_firestore_client() -> FirestoreClient:
    return FirestoreClient(get_settings())