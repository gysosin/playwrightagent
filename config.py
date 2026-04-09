"""Application configuration loaded from environment variables."""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Project settings populated from environment variables.

    Reads a `.env` file automatically when present in the working directory.
    Every field can be overridden by an environment variable of the same
    (case-insensitive) name.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- OpenRouter / LLM -------------------------------------------------
    OPENROUTER_API_KEY: SecretStr
    OPENROUTER_MODEL: str = "openai/gpt-4o"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # --- PostgreSQL --------------------------------------------------------
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "admin"
    POSTGRES_PASSWORD: SecretStr = SecretStr("admin123")
    POSTGRES_DB: str = "adk_automation"

    # --- MinIO -------------------------------------------------------------
    MINIO_HOST: str = "localhost"
    MINIO_PORT: int = 9000
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: SecretStr = SecretStr("minioadmin123")
    MINIO_BUCKET: str = "adk-snapshots"

    # --- Playwright MCP ----------------------------------------------------
    PLAYWRIGHT_MCP_URL: str = "http://localhost:8931/sse"

    # --- Derived properties ------------------------------------------------

    @property
    def postgres_dsn(self) -> str:
        """Return an asyncpg-compatible PostgreSQL DSN."""
        return (
            f"postgresql://{quote_plus(self.POSTGRES_USER)}:{quote_plus(self.POSTGRES_PASSWORD.get_secret_value())}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def minio_endpoint(self) -> str:
        """Return the MinIO endpoint as ``host:port``."""
        return f"{self.MINIO_HOST}:{self.MINIO_PORT}"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton instance of :class:`Settings`."""
    return Settings()
