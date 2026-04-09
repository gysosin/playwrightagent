"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Project settings populated from environment variables.

    Reads a `.env` file automatically when present in the working directory.
    Every field can be overridden by an environment variable of the same
    (case-insensitive) name.
    """

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # --- OpenRouter / LLM -------------------------------------------------
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "openai/gpt-4o"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # --- PostgreSQL --------------------------------------------------------
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "admin"
    POSTGRES_PASSWORD: str = "admin123"
    POSTGRES_DB: str = "adk_automation"

    # --- MinIO -------------------------------------------------------------
    MINIO_HOST: str = "localhost"
    MINIO_PORT: int = 9000
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin123"
    MINIO_BUCKET: str = "adk-snapshots"

    # --- Derived properties ------------------------------------------------

    @property
    def postgres_dsn(self) -> str:
        """Return an asyncpg-compatible PostgreSQL DSN."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def minio_endpoint(self) -> str:
        """Return the MinIO endpoint as ``host:port``."""
        return f"{self.MINIO_HOST}:{self.MINIO_PORT}"
