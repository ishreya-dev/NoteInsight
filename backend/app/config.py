"""Application configuration loaded from environment variables."""
from dotenv import load_dotenv

load_dotenv()

import os
from functools import lru_cache


class MissingConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings:
    """Process settings loaded once from the environment."""

    def __init__(self) -> None:
        self.gemini_api_key = self._require("GEMINI_API_KEY")
        self.gemini_model = self._optional(
            "GEMINI_MODEL",
            default="gemini-3.6-flash",
        )

        self.firebase_service_account_path = self._optional_path(
            "FIREBASE_SERVICE_ACCOUNT_PATH"
        )
        self.firebase_project_id = self._require("FIREBASE_PROJECT_ID")

        self.environment = self._optional("ENVIRONMENT", default="development")
        self.allowed_origins = self._parse_origins(
            os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
        )

    @staticmethod
    def _require(key: str) -> str:
        value = os.getenv(key)
        if value is None or not value.strip():
            raise MissingConfigError(
                f"Required environment variable '{key}' is not set."
            )
        return value.strip()

    @staticmethod
    def _optional(key: str, *, default: str) -> str:
        value = os.getenv(key)
        if value is None or not value.strip():
            return default
        return value.strip()

    @staticmethod
    def _optional_path(key: str) -> str | None:
        value = os.getenv(key)
        if value is None or not value.strip():
            return None

        path = value.strip()
        if not os.path.isfile(path):
            raise MissingConfigError(
                f"Environment variable '{key}' does not point to an existing file."
            )
        return path

    @staticmethod
    def _parse_origins(value: str | None) -> list[str]:
        if value is None:
            raise MissingConfigError(
                "ALLOWED_ORIGINS must contain at least one valid origin."
            )

        origins = [
            origin.strip()
            for origin in value.split(",")
            if origin.strip()
        ]
        if not origins:
            raise MissingConfigError(
                "ALLOWED_ORIGINS must contain at least one valid origin."
            )
        return origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
