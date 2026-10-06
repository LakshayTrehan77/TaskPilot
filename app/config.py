from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://taskpilot:taskpilot@localhost:5432/taskpilot"
    redis_url: str = ""
    log_level: str = "INFO"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    bcrypt_rounds: int = 12

    task_cache_ttl_seconds: int = 30
    plan_generation_timeout_seconds: int = 300

    ai_mode: Literal["mock", "llm"] = "mock"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-flash"

    @model_validator(mode="after")
    def check_secrets(self):
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")

        if self.ai_mode == "llm" and not self.llm_api_key:
            raise ValueError("LLM_API_KEY is required when AI_MODE=llm")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()