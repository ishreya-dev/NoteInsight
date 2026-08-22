"""Firebase ID token verification for FastAPI dependencies."""

from __future__ import annotations

import logging

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from firebase_admin.exceptions import FirebaseError
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models.user import AuthenticatedUser
from app.services.firestore_client import FirestoreClient, get_firestore_client

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


def _init_firebase_app() -> None:
    """Initialize Firebase Admin once per process."""
    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass

    settings = get_settings()

    try:
        if settings.firebase_service_account_path:
            credential = credentials.Certificate(
                settings.firebase_service_account_path
            )
        else:
            credential = credentials.ApplicationDefault()

        firebase_admin.initialize_app(
            credential,
            {"projectId": settings.firebase_project_id},
        )
    except Exception as exc:
        logger.exception("Firebase initialization failed")
        raise RuntimeError("Firebase initialization failed") from exc


def _invalid_credentials() -> HTTPException:
    """Return a standard authentication failure response."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


_init_firebase_app()


async def get_current_user(
    auth_credentials: HTTPAuthorizationCredentials | None = Depends(
        _bearer_scheme
    ),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """Return the caller identity from a verified Firebase ID token."""

    if auth_credentials is None or not auth_credentials.credentials.strip():
        raise _invalid_credentials()

    try:
        decoded_token = firebase_auth.verify_id_token(
            auth_credentials.credentials,
            check_revoked=settings.firebase_check_revoked_tokens,
        )
    except (ValueError, FirebaseError) as exc:
        logger.warning(
            "Firebase token verification failed: %s",
            type(exc).__name__,
        )
        raise _invalid_credentials() from exc

    uid = decoded_token.get("uid")

    if not isinstance(uid, str) or not uid.strip():
        raise _invalid_credentials()

    try:
        return AuthenticatedUser(
            uid=uid,
            email=decoded_token.get("email"),
        )
    except ValidationError as exc:
        raise _invalid_credentials() from exc


async def enforce_analysis_rate_limit(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: FirestoreClient = Depends(get_firestore_client),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce the configured per-user analysis request limit."""
    try:
        allowed = await db.consume_rate_limit_slot(
            user_id=current_user.uid,
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    except Exception:
        logger.exception(
            "Rate limit check failed for user %s; allowing request",
            current_user.uid,
        )
        # Deliberate fail-open: an infra hiccup here must not block the
        # core note-submission flow for all users. This trades strict
        # cost-guarding for availability. Gemini's own 429 handling
        # (gemini_client.py) provides a second layer of defense against
        # runaway request volume, so this is not the only safeguard.
        return

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many analysis requests. Please wait before "
                "submitting another note."
            ),
        )