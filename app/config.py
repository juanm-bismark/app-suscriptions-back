from functools import lru_cache
from typing import Annotated, Any, Optional

from pydantic import AliasChoices, Field, PlainValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors_origins(value: Any) -> list[str]:
    """Parse CORS origins from env var (comma-separated) or list."""
    # Handle empty, None, or missing values — default to development origins
    if not value or (isinstance(value, str) and not value.strip()):
        return ["http://localhost:3000", "http://localhost:5173"]
    # Parse comma-separated string
    if isinstance(value, str):
        parsed = [o.strip() for o in value.split(",") if o.strip()]
        return parsed if parsed else ["http://localhost:3000", "http://localhost:5173"]
    # Already a list
    if isinstance(value, list):
        return value
    return ["http://localhost:3000", "http://localhost:5173"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Make sensitive settings optional so Settings() can be created
    # even if environment variables are not present during some workflows.
    database_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "DATABASE_URL_DOCKER"),
    )
    jwt_secret: Optional[str] = None
    jwt_expire_minutes: int = 60
    environment: str = "development"
    database_echo: bool = False
    # Use `Any` for the annotated type so the dotenv/settings source does not
    # attempt JSON decoding on the raw env string. `PlainValidator` will
    # convert whatever value (empty, comma-separated string, or list) into
    # the expected list[str].
    cors_origins: Annotated[Any, PlainValidator(parse_cors_origins)] = ["http://localhost:3000", "http://localhost:5173"]
    fernet_key: Optional[str] = None  # 32-byte URL-safe base64 key — generate with: Fernet.generate_key().decode()
    # Feature flag to enable lifecycle write operations (set status, purge, retire)
    # Default: disabled to prevent accidental writes in non-sandbox environments.
    lifecycle_writes_enabled: bool = False

@lru_cache
def get_settings() -> Settings:
    return Settings()
