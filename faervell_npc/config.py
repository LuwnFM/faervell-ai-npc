from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_ACTOR_MODELS = [
    # This is only a preferred order, not a free-model allowlist. The live OpenRouter
    # catalogue contributes every other free text model that is not explicitly blocked.
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "deepseek/deepseek-v4-flash",
]
DEFAULT_PLANNER_MODELS = [
    "deepseek/deepseek-v4-flash",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
]
DEFAULT_MODEL_BLOCKLIST = [
    "openrouter/free",
    "openrouter/auto",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-nano-9b-v2",
    "laguna-2.1-xs",
    "laguna-2-1-xs",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    discord_guild_id: int | None = None
    discord_gm_role_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    discord_admin_channel_id: int | None = None
    discord_gm_review_channel_id: int | None = None
    discord_character_registry_channel_id: int | None = 707461395209256982
    character_registry_auto_sync_enabled: bool = True
    character_registry_sync_interval_hours: int = Field(default=48, ge=24, le=168)
    discord_command_prefix: str = "!"

    database_url: str = "postgresql+asyncpg://faervell:faervell@localhost:5432/faervell"
    redis_url: str = "redis://localhost:6379/0"
    auto_create_schema: bool = True

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "Faervell Stranger NPC"
    actor_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ACTOR_MODELS)
    )
    planner_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_PLANNER_MODELS)
    )
    model_blocklist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MODEL_BLOCKLIST)
    )
    openrouter_allow_paid_fallback: bool = True
    openrouter_dynamic_catalog: bool = True
    openrouter_catalog_ttl_seconds: int = Field(default=1800, ge=60)
    openrouter_max_catalog_candidates: int = Field(default=24, ge=3, le=100)
    openrouter_max_prompt_price_per_million: float = Field(default=0.20, ge=0.0)
    openrouter_max_completion_price_per_million: float = Field(default=0.20, ge=0.0)
    openrouter_max_request_price_usd: float = Field(default=0.0, ge=0.0)
    openrouter_planner_reasoning_effort: str = "high"
    actor_max_tokens: int = 1000
    openrouter_response_timeout_seconds: int = Field(default=180, ge=30, le=600)
    actor_quality_attempts: int = Field(default=3, ge=1, le=6)
    planner_max_tokens: int = 1600
    planner_daily_budget_usd: float = 2.0
    planner_escalation_enabled: bool = True

    # Traveler Memory v2 / 1.0.0 feature gates.  The defaults are safe for a
    # staged rollout: local writes and reads can be enabled independently.
    traveler_memory_v2_enabled: bool = True
    traveler_memory_v2_write_enabled: bool = True
    traveler_memory_v2_read_enabled: bool = True
    memory_personal_enabled: bool = True
    memory_testimony_enabled: bool = True
    memory_world_testimony_enabled: bool = True
    memory_claims_enabled: bool = True
    memory_evidence_enabled: bool = True
    memory_adaptive_context_enabled: bool = True
    memory_context_reserve_mode: str = "adaptive"
    memory_include_low_score_when_space: bool = True
    memory_dedup_vector_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    memory_dedup_lexical_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    memory_candidate_pool: int = Field(default=64, ge=8, le=500)
    memory_graph_enabled: bool = False
    memory_llm_update_enabled: bool = False
    memory_background_life_enabled: bool = False
    memory_allow_cross_character_testimony: bool = True
    memory_require_attribution: bool = True
    memory_multiple_sources_confirm: bool = False
    memory_canon_overrides_testimony: bool = True
    model_context_length: int = Field(default=8192, ge=1024)

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
    discord_reply_hint_text: str = (
        "Чтобы продолжить разговор со Странником, упомяните его в своём сообщении "
        "или ответьте на один из его постов."
    )
    traveler_presence_enabled: bool = True
    traveler_movement_interval_seconds: int = Field(default=600, ge=30)
    # A queued summons never interrupts an active RP scene. Each handled
    # interaction extends this lease; the movement loop may leave only after
    # the scene has been quiet for this grace period (or a GM explicitly ends it).
    traveler_scene_settle_seconds: int = Field(default=900, ge=0, le=86400)
    traveler_default_appearance_probability: float = Field(default=0.20, ge=0.0, le=1.0)
    traveler_summon_move_chance: float = Field(default=0.75, ge=0.0, le=1.0)
    traveler_cross_location_min_score: float = Field(default=0.58, ge=0.0, le=1.0)
    traveler_auto_register_locations: bool = True
    traveler_enforce_startup_lock: bool = True
    traveler_startup_lock_channel_id: int | None = 1488544832950374481
    traveler_rp_category_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [
            682909341300293662,
            1057679719597879437,
            1133768572510941276,
            1255157727278403614,
            1426883198327193640,
            1057717821552984194,
            1459852302071631988,
        ]
    )
    traveler_events_category_id: int | None = 1058403455934398495
    traveler_manual_only_category_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [730030732185043004, 1490668605594013776]
    )
    knowledge_auto_ingest: bool = True
    knowledge_min_wiki_documents: int = Field(default=669, ge=1)
    knowledge_stale_hours: int = Field(default=24, ge=1)
    fandom_api_concurrency: int = Field(default=4, ge=1, le=12)
    fandom_batch_size: int = Field(default=40, ge=1, le=50)
    quest_default_reward_amount: float = Field(default=5.0, ge=0.0, le=12.0)
    quest_default_reward_currency: str = "местных монет"
    discord_model_footer_enabled: bool = True
    discord_regeneration_limit: int = Field(default=1, ge=0, le=20)
    character_match_threshold: float = 0.22
    character_match_margin: float = 0.04

    behavior_pack_path: Path = Path("behavior-pack")
    template_library_path: Path = Path("behavior-pack/template-library")
    data_path: Path = Path("data")

    @field_validator(
        "discord_guild_id",
        "discord_admin_channel_id",
        "discord_gm_review_channel_id",
        "discord_character_registry_channel_id",
        "traveler_events_category_id",
        "traveler_startup_lock_channel_id",
        mode="before",
    )
    @classmethod
    def parse_optional_int(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("openrouter_planner_reasoning_effort", mode="before")
    @classmethod
    def parse_reasoning_effort(cls, value: object) -> str:
        effort = str(value or "").strip().casefold()
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
        if effort not in allowed:
            raise ValueError(
                "OPENROUTER_PLANNER_REASONING_EFFORT must be one of: " + ", ".join(sorted(allowed))
            )
        return effort

    @field_validator(
        "discord_gm_role_ids",
        "actor_models",
        "planner_models",
        "model_blocklist",
        "traveler_rp_category_ids",
        "traveler_manual_only_category_ids",
        mode="before",
    )
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

    def filter_allowed_models(self, models: list[str]) -> list[str]:
        """Return a stable preferred list after applying only the explicit blocklist."""
        blocked = [item.casefold().strip() for item in self.model_blocklist if item.strip()]
        accepted: list[str] = []
        seen: set[str] = set()
        for raw_model in models:
            model = raw_model.strip()
            folded = model.casefold()
            if not model or folded in seen:
                continue
            if any(token in folded for token in blocked):
                continue
            if not self.openrouter_allow_paid_fallback and not folded.endswith(":free"):
                continue
            seen.add(folded)
            accepted.append(model)
        return accepted

    @property
    def effective_actor_models(self) -> list[str]:
        return self.filter_allowed_models(self.actor_models)

    @property
    def effective_planner_models(self) -> list[str]:
        return self.filter_allowed_models(self.planner_models)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
