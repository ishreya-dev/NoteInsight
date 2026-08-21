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

from app.config import get_settings
from app.models.user import AuthenticatedUser

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


_init_firebase_app()


async def get_current_user(
    auth_credentials: HTTPAuthorizationCredentials | None = Depends(
        _bearer_scheme
    ),
) -> AuthenticatedUser:
    """Return the caller identity from a verified Firebase ID token only."""

    if auth_credentials is None or not auth_credentials.credentials.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        decoded_token = firebase_auth.verify_id_token(
            auth_credentials.credentials,
            check_revoked=True,
        )
    except (ValueError, FirebaseError) as exc:
        # Cover invalid/expired/revoked tokens and related Firebase failures.
        # Never return provider error details to the client.
        logger.warning(
            "Firebase token verification failed: %s - %s",
            type(exc).__name__,
            str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    uid = decoded_token.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return AuthenticatedUser(
            uid=uid,
            email=decoded_token.get("email"),
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
