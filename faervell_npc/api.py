from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from faervell_npc.config import get_settings
from faervell_npc.db import SessionLocal, init_db
from faervell_npc.runtime import Runtime, build_runtime


def create_app(
    runtime: Runtime | None = None,
    *,
    manage_runtime: bool = True,
    initialize_schema: bool = True,
) -> FastAPI:
    owned_runtime = runtime or build_runtime()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if initialize_schema and settings.auto_create_schema:
            await init_db()
        yield
        if manage_runtime:
            await owned_runtime.close()

    app = FastAPI(
        title="Faervell AI-NPC",
        version="0.7.4",
        description="Health and operational API for the Discord Stranger NPC.",
        lifespan=lifespan,
    )
    app.state.runtime = owned_runtime

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str | bool]:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "llm_enabled": settings.llm_enabled,
            "planner_escalation": settings.planner_escalation_enabled,
        }

    return app

