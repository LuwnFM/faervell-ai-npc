from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from faervell_npc.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from faervell_npc import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight forward-compatible migrations for existing MVP databases.
        # create_all() creates new tables but does not add columns to existing ones.
        await conn.execute(
            text(
                "ALTER TABLE scene_configs "
                "ADD COLUMN IF NOT EXISTS reply_hint_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE scene_configs "
                "ADD COLUMN IF NOT EXISTS appearance_probability DOUBLE PRECISION "
                "NOT NULL DEFAULT 0.20"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE scene_configs "
                "ADD COLUMN IF NOT EXISTS arrival_announcement_enabled BOOLEAN "
                "NOT NULL DEFAULT TRUE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS event_locations_enabled BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS movement_locked BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS locked_channel_id VARCHAR(32)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS startup_lock_released BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS current_scene_engaged_until TIMESTAMPTZ"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE traveler_presence "
                "ADD COLUMN IF NOT EXISTS last_interaction_at TIMESTAMPTZ"
            )
        )
        await conn.execute(text("ALTER TABLE scene_configs ADD COLUMN IF NOT EXISTS category_id VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE scene_configs ADD COLUMN IF NOT EXISTS category_name VARCHAR(256)"))
        await conn.execute(text("ALTER TABLE scene_configs ADD COLUMN IF NOT EXISTS location_path VARCHAR(512)"))
        await conn.execute(
            text(
                "ALTER TABLE scene_configs ADD COLUMN IF NOT EXISTS "
                "automatic_appearance_allowed BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        await conn.execute(text("ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS http_status INTEGER"))
        await conn.execute(text("ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS selection_reason TEXT"))
        await conn.execute(
            text("ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS request_metadata JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text(
                "ALTER TABLE character_profiles ADD COLUMN IF NOT EXISTS "
                "sheet_fields JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        # Memory v2 is additive and idempotent. Existing 0.8 rows remain valid.
        memory_columns = {
            "traveler_entity_id": "VARCHAR(64) NOT NULL DEFAULT 'traveler_01'",
            "scope_type": "VARCHAR(32) NOT NULL DEFAULT 'PERSONAL'",
            "normalized_content": "TEXT NOT NULL DEFAULT ''",
            "content_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
            "claim_id": "VARCHAR(36)",
            "speaker_character_id": "VARCHAR(128)",
            "speaker_display_name": "VARCHAR(256)",
            "subject_character_ids": "JSONB NOT NULL DEFAULT '[]'::jsonb",
            "subject_entity_keys": "JSONB NOT NULL DEFAULT '[]'::jsonb",
            "participant_character_ids": "JSONB NOT NULL DEFAULT '[]'::jsonb",
            "mentioned_dates": "JSONB NOT NULL DEFAULT '[]'::jsonb",
            "novelty_score": "DOUBLE PRECISION NOT NULL DEFAULT 1.0",
            "reinforcement_count": "INTEGER NOT NULL DEFAULT 0",
            "corroboration_count": "INTEGER NOT NULL DEFAULT 0",
            "access_count": "INTEGER NOT NULL DEFAULT 0",
            "last_accessed_at": "TIMESTAMPTZ",
            "last_reinforced_at": "TIMESTAMPTZ",
            "is_anchor": "BOOLEAN NOT NULL DEFAULT FALSE",
            "is_cherished": "BOOLEAN NOT NULL DEFAULT FALSE",
            "lifecycle_status": "VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'",
            "why_saved": "TEXT NOT NULL DEFAULT ''",
            "attribution_mode": "VARCHAR(24) NOT NULL DEFAULT 'ATTRIBUTABLE'",
            "disclosure_scope": "VARCHAR(24) NOT NULL DEFAULT 'PUBLIC'",
            "confidentiality": "VARCHAR(24) NOT NULL DEFAULT 'PUBLIC'",
            "source_message_id": "VARCHAR(32)",
            "source_quest_id": "VARCHAR(36)",
            "source_scene_id": "VARCHAR(64)",
            "source_location_id": "VARCHAR(128)",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        }
        for column, definition in memory_columns.items():
            await conn.execute(
                text(
                    "ALTER TABLE traveler_character_memories "
                    f"ADD COLUMN IF NOT EXISTS {column} {definition}"
                )
            )
        for column, definition in {"corroboration_count": "INTEGER NOT NULL DEFAULT 0"}.items():
            await conn.execute(
                text(
                    "ALTER TABLE memory_claims "
                    f"ADD COLUMN IF NOT EXISTS {column} {definition}"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_character_lifecycle "
                "ON traveler_character_memories(character_id, lifecycle_status)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_scope_lifecycle "
                "ON traveler_character_memories(scope_type, lifecycle_status)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_normalized_content_russian "
                "ON traveler_character_memories USING GIN "
                "(to_tsvector('russian', normalized_content))"
            )
        )
        for index_name, column in (
            ("ix_memory_subject_characters_gin", "subject_character_ids"),
            ("ix_memory_subject_entities_gin", "subject_entity_keys"),
            ("ix_memory_participants_gin", "participant_character_ids"),
        ):
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON traveler_character_memories USING GIN ({column})"
                )
            )
        await conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION forbid_conversation_message_mutation()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'conversation_messages is append-only';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await conn.execute(
            text(
                "DROP TRIGGER IF EXISTS conversation_messages_append_only "
                "ON conversation_messages"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER conversation_messages_append_only "
                "BEFORE UPDATE OR DELETE ON conversation_messages "
                "FOR EACH ROW EXECUTE FUNCTION forbid_conversation_message_mutation()"
            )
        )


async def close_db() -> None:
    await engine.dispose()
