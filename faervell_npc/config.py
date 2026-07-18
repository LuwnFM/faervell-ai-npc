from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    discord_guild_id: int | None = None
    discord_gm_role_ids: list[int] = Field(default_factory=list)
    discord_admin_channel_id: int | None = None
    discord_command_prefix: str = "!"

    database_url: str = "postgresql+asyncpg://faervell:faervell@localhost:5432/faervell"
    redis_url: str = "redis://localhost:6379/0"
    auto_create_schema: bool = True

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "Faervell Stranger NPC"
    actor_models: list[str] = Field(default_factory=lambda: ["openrouter/free"])
    planner_models: list[str] = Field(
        default_factory=lambda: ["openai/gpt-5-nano", "google/gemini-2.5-flash-lite"]
    )
    actor_max_tokens: int = 650
    planner_max_tokens: int = 1600
    planner_daily_budget_usd: float = 2.0
    planner_escalation_enabled: bool = True

    log_level: str = "INFO"
    default_language: str = "ru"
    pseudonym_secret: str = "change-me-in-production"
    embedding_provider: str = "hashing"
    embedding_dimensions: int = 384
    semantic_model: str = "intfloat/multilingual-e5-small"
    max_recent_messages: int = 24
    max_retrieved_memories: int = 5
    max_retrieved_knowledge: int = 6
    bot_reply_cooldown_seconds: int = 2

    behavior_pack_path: Path = Path("behavior-pack")
    data_path: Path = Path("data")


    @field_validator("discord_guild_id", "discord_admin_channel_id", mode="before")
    @classmethod
    def parse_optional_int(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("discord_gm_role_ids", "actor_models", "planner_models", mode="before")
    @classmethod
    def parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return []
            items = [part.strip() for part in value.split(",") if part.strip()]
            if all(item.isdigit() for item in items):
                return [int(item) for item in items]
            return items
        return value

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
