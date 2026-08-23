"""Pydantic models for authenticated and stored users."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.base import utcnow


class User(BaseModel):
    """User profile stored in Firestore, when used."""

    uid: str = Field(min_length=1)
    email: EmailStr
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("uid")
    @classmethod
    def validate_uid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("uid cannot be empty or whitespace only")
        return value


class AuthenticatedUser(BaseModel):
    """User identity derived only from a verified Firebase ID token."""

    uid: str = Field(min_length=1)
    # Plain str: token emails are not always RFC-strict; auth must not 500 on format.
    email: str | None = None

    @field_validator("uid")
    @classmethod
    def validate_uid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("uid cannot be empty or whitespace only")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
