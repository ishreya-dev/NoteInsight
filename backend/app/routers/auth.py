"""Authentication-related endpoints."""

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.models.user import AuthenticatedUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=AuthenticatedUser)
async def get_me(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Return the authenticated user from a verified Firebase ID token."""
    return current_user
