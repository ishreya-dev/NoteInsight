"""Shared pytest fixtures and test bootstrap.

Stubs provider SDKs so tests run without live Firebase, Firestore, or Gemini.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("FIREBASE_PROJECT_ID", "demo-project")
os.environ.setdefault("ALLOWED_ORIGINS", "http://testserver")
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_PATH", None)


def _ensure_module(name: str) -> ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    return module


for module_name in (
    "google",
    "google.cloud",
    "google.cloud.firestore",
    "google.cloud.firestore_v1",
    "google.cloud.firestore_v1.async_client",
    "google.cloud.firestore_v1.base_query",
    "google.genai",
    "google.genai.types",
    "firebase_admin",
    "firebase_admin.auth",
    "firebase_admin.credentials",
    "firebase_admin.exceptions",
):
    _ensure_module(module_name)

firestore_mod = sys.modules["google.cloud.firestore"]
firestore_mod.Query = MagicMock(DESCENDING="DESCENDING")
firestore_mod.async_transactional = lambda fn: fn

sys.modules["google.cloud.firestore_v1.base_query"].FieldFilter = MagicMock
sys.modules["google.cloud.firestore_v1.async_client"].AsyncClient = MagicMock

firebase_admin = sys.modules["firebase_admin"]
firebase_admin.get_app = MagicMock(return_value=MagicMock())
firebase_admin.initialize_app = MagicMock(return_value=MagicMock())
firebase_admin.credentials = sys.modules["firebase_admin.credentials"]
firebase_admin.exceptions = sys.modules["firebase_admin.exceptions"]
firebase_admin.auth = sys.modules["firebase_admin.auth"]
sys.modules["firebase_admin.credentials"].Certificate = MagicMock()
sys.modules["firebase_admin.credentials"].ApplicationDefault = MagicMock()
sys.modules["firebase_admin.auth"].FirebaseError = type(
    "FirebaseError",
    (Exception,),
    {},
)
sys.modules["firebase_admin.exceptions"].FirebaseError = type(
    "FirebaseError",
    (Exception,),
    {},
)
sys.modules["firebase_admin.auth"].verify_id_token = MagicMock()

sys.modules["google.genai"].Client = MagicMock()
sys.modules["google.genai.types"].GenerateContentConfig = MagicMock()


# ---- Shared domain builders for API tests ----

from app.dependencies import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.models.analysis import (  # noqa: E402
    Analysis,
    Condition,
    DocumentationStatus,
    Review,
)
from app.models.note import Note, ReviewStatus  # noqa: E402
from app.models.user import AuthenticatedUser  # noqa: E402
from app.services.firestore_client import get_firestore_client  # noqa: E402
from app.services.gemini_client import AnalysisResult, get_gemini_client  # noqa: E402

TEST_USER = AuthenticatedUser(uid="user-1", email="clinician@example.com")


def make_condition(condition_id: str = "c1") -> Condition:
    return Condition(
        id=condition_id,
        condition_name="Type 2 diabetes mellitus",
        evidence_quote="Type 2 diabetes, controlled",
        documentation_status=DocumentationStatus.WELL_DOCUMENTED,
        suggested_icd10="E11.9",
        confidence=0.9,
        quote_verified=True,
    )


def make_analysis(
    *,
    analysis_id: str = "a1",
    note_id: str = "n1",
    user_id: str = "user-1",
    failed: bool = False,
) -> Analysis:
    if failed:
        return Analysis(
            id=analysis_id,
            note_id=note_id,
            user_id=user_id,
            conditions=(),
            gaps=(),
            summary="Analysis could not be completed. Please try again.",
            model_version="gemini-test",
            prompt_version="v1",
            is_failed=True,
            failure_reason="Gemini did not return valid output after 2 attempts",
        )
    return Analysis(
        id=analysis_id,
        note_id=note_id,
        user_id=user_id,
        conditions=(make_condition(),),
        gaps=(),
        summary="Follow-up for diabetes",
        model_version="gemini-test",
        prompt_version="v1",
    )


def make_note(
    *,
    note_id: str = "n1",
    user_id: str = "user-1",
    latest_analysis_id: str | None = None,
    review_status: ReviewStatus = ReviewStatus.PENDING,
    condition_count: int = 0,
) -> Note:
    return Note(
        id=note_id,
        user_id=user_id,
        raw_text="Patient has Type 2 diabetes, controlled on metformin.",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        latest_analysis_id=latest_analysis_id,
        review_status=review_status,
        condition_count=condition_count,
    )


def make_review(*, analysis_id: str = "a1", note_id: str = "n1") -> Review:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Review(
        id=analysis_id,
        analysis_id=analysis_id,
        note_id=note_id,
        user_id=TEST_USER.uid,
        conditions=[],
        gaps=[],
        created_at=now,
        updated_at=now,
    )


def make_analysis_result() -> AnalysisResult:
    return AnalysisResult(
        conditions=[make_condition()],
        gaps=[],
        summary="Follow-up for diabetes",
        model_version="gemini-test",
        prompt_version="v1",
    )


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.create_note = AsyncMock()
    mock.get_note = AsyncMock()
    mock.list_notes = AsyncMock(return_value=[])
    mock.get_analysis = AsyncMock()
    mock.get_review_for_analysis = AsyncMock(return_value=None)
    mock.persist_analysis_for_note = AsyncMock()
    mock.create_analysis_job = AsyncMock()
    mock.claim_analysis_job = AsyncMock(return_value="claimed")
    mock.get_analysis_job = AsyncMock()
    mock.finish_analysis_job = AsyncMock()
    mock.upsert_review = AsyncMock()
    return mock


@pytest.fixture
def gemini() -> AsyncMock:
    mock = AsyncMock()
    mock.analyze_note = AsyncMock(return_value=make_analysis_result())
    return mock


@pytest.fixture
def client(db: AsyncMock, gemini: AsyncMock):
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[get_firestore_client] = lambda: db
    app.dependency_overrides[get_gemini_client] = lambda: gemini

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
