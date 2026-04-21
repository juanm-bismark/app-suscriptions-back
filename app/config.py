from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
