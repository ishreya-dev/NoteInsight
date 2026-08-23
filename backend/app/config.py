"""Application configuration loaded from environment variables."""

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


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

        self.environment = self._optional(
            "ENVIRONMENT",
            default="development",
        )
        self.allowed_origins = self._parse_origins(
            os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
        )

        self.rate_limit_max_requests = self._optional_int(
            "RATE_LIMIT_MAX_REQUESTS",
            default=30,
        )
        self.rate_limit_window_seconds = self._optional_int(
            "RATE_LIMIT_WINDOW_SECONDS",
            default=3600,
        )

        self.analysis_timeout_seconds = self._optional_int(
            "ANALYSIS_TIMEOUT_SECONDS",
            default=60,
        )

        # Controls whether revoked Firebase tokens are rejected.
        self.firebase_check_revoked_tokens = self._optional_bool(
            "FIREBASE_CHECK_REVOKED_TOKENS",
            default=True,
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
                f"Environment variable '{key}' does not point to an "
                "existing file."
            )
        return path

    @staticmethod
    def _optional_int(key: str, *, default: int) -> int:
        value = os.getenv(key)
        if value is None or not value.strip():
            return default

        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise MissingConfigError(
                f"Environment variable '{key}' must be an integer."
            ) from exc

        if parsed <= 0:
            raise MissingConfigError(
                f"Environment variable '{key}' must be a positive integer."
            )

        return parsed

    @staticmethod
    def _optional_bool(key: str, *, default: bool) -> bool:
        value = os.getenv(key)
        if value is None or not value.strip():
            return default

        normalized = value.strip().lower()

        if normalized in {"1", "true", "yes", "on"}:
            return True

        if normalized in {"0", "false", "no", "off"}:
            return False

        raise MissingConfigError(
            f"Environment variable '{key}' must be a boolean-like value."
        )

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