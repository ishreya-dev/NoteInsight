"""Authentication API behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.main import app
from tests.conftest import TEST_USER


def test_health_does_not_require_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_me_returns_authenticated_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as client:
        response = client.get("/auth/me")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["uid"] == TEST_USER.uid
    assert response.json()["email"] == TEST_USER.email


def test_auth_me_without_credentials_returns_401() -> None:
    # No dependency override: real auth dependency rejects missing bearer token.
    with TestClient(app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired credentials"


def test_protected_endpoint_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicit invalid bearer token — distinct from missing credentials.
    import app.dependencies as dependencies

    monkeypatch.setattr(
        dependencies.firebase_auth,
        "verify_id_token",
        MagicMock(side_effect=ValueError("bad token")),
    )

    with TestClient(app) as client:
        response = client.get(
            "/auth/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired credentials"
    dependencies.firebase_auth.verify_id_token.assert_called_once_with(
        "not-a-real-token",
        check_revoked=True,
    )


def test_firebase_credential_failure_is_categorized_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.dependencies as dependencies

    original_error = RuntimeError("invalid service account")
    monkeypatch.setattr(
        dependencies.firebase_admin,
        "get_app",
        MagicMock(side_effect=ValueError("app is not initialized")),
    )
    monkeypatch.setattr(
        dependencies,
        "get_settings",
        lambda: MagicMock(
            firebase_service_account_path="service-account.json",
            firebase_project_id="demo-project",
        ),
    )
    monkeypatch.setattr(
        dependencies.credentials,
        "Certificate",
        MagicMock(side_effect=original_error),
    )

    with pytest.raises(RuntimeError, match="Firebase initialization failed") as exc_info:
        dependencies._init_firebase_app()

    assert exc_info.value.__cause__ is original_error


def test_firebase_initialize_failure_is_categorized_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.dependencies as dependencies

    original_error = RuntimeError("firebase provider unavailable")
    monkeypatch.setattr(
        dependencies.firebase_admin,
        "get_app",
        MagicMock(side_effect=ValueError("app is not initialized")),
    )
    monkeypatch.setattr(
        dependencies,
        "get_settings",
        lambda: MagicMock(
            firebase_service_account_path=None,
            firebase_project_id="demo-project",
        ),
    )
    monkeypatch.setattr(
        dependencies.credentials,
        "ApplicationDefault",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        dependencies.firebase_admin,
        "initialize_app",
        MagicMock(side_effect=original_error),
    )

    with pytest.raises(RuntimeError, match="Firebase initialization failed") as exc_info:
        dependencies._init_firebase_app()

    assert exc_info.value.__cause__ is original_error