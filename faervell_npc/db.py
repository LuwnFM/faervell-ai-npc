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
