from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import Body, FastAPI, HTTPException
from sqlalchemy import text

from faervell_npc.config import get_settings
from faervell_npc.db import SessionLocal, init_db
from faervell_npc.models import MemoryClaim, TravelerMemory
from faervell_npc.runtime import Runtime, build_runtime
from faervell_npc.services.memory.schemas import CortexRenderBudget, MemoryRecallQuery, MemoryRoute


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
        version="1.0.0",
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

    async def _internal_memory(memory_id: str) -> TravelerMemory:
        async with SessionLocal() as session:
            item = await session.get(TravelerMemory, memory_id)
            if item is None:
                raise HTTPException(status_code=404, detail="memory not found")
            return item

    @app.get("/internal/memory/character/{character_id}")
    async def memory_for_character(character_id: str) -> list[dict[str, object]]:
        async with SessionLocal() as session:
            rows = (await session.execute(
                text(
                    "SELECT id, character_id, scope_type, statement, trust_status, importance, "
                    "speaker_character_id, speaker_display_name, lifecycle_status, is_anchor, is_cherished "
                    "FROM traveler_character_memories WHERE character_id = :character_id "
                    "AND lifecycle_status <> 'REDACTED' ORDER BY importance DESC"
                ), {"character_id": character_id}
            )).mappings().all()
            return [dict(row) for row in rows]

    @app.get("/internal/memory/{memory_id}")
    async def memory_detail(memory_id: str) -> dict[str, object]:
        item = await _internal_memory(memory_id)
        return {key: value for key, value in item.__dict__.items() if not key.startswith("_")}

    @app.patch("/internal/memory/{memory_id}")
    async def memory_patch(memory_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
        async with SessionLocal() as session:
            item = await session.get(TravelerMemory, memory_id)
            if item is None:
                raise HTTPException(status_code=404, detail="memory not found")
            for key in ("importance", "why_saved", "attribution_mode", "disclosure_scope", "confidentiality"):
                if key in payload:
                    setattr(item, key, payload[key])
            await owned_runtime.orchestrator.memory.cortex.mark_dirty(
                session, item.character_id, "memory:manual_patch"
            )
            await session.commit()
            return {key: value for key, value in item.__dict__.items() if not key.startswith("_")}

    async def _memory_action(memory_id: str, action: str) -> dict[str, object]:
        async with SessionLocal() as session:
            item = await session.get(TravelerMemory, memory_id)
            if item is None:
                raise HTTPException(status_code=404, detail="memory not found")
            service = owned_runtime.orchestrator.memory
            if action == "anchor":
                await service.writer.anchor(session, memory_id, True)
            elif action == "unanchor":
                await service.writer.anchor(session, memory_id, False)
            elif action == "cherish":
                await service.writer.cherish(session, memory_id, True)
            elif action == "uncherish":
                await service.writer.cherish(session, memory_id, False)
            elif action == "archive":
                await service.archive.archive(session, memory_id)
            elif action == "restore":
                await service.archive.restore(session, memory_id)
            elif action == "redact":
                await service.archive.redact(session, memory_id, "admin")
            await session.commit()
            return {key: value for key, value in item.__dict__.items() if not key.startswith("_")}

    for _action in ("anchor", "unanchor", "cherish", "uncherish", "archive", "restore", "redact"):
        async def _handler(memory_id: str, action: str = _action) -> dict[str, object]:
            return await _memory_action(memory_id, action)
        app.add_api_route(f"/internal/memory/{{memory_id}}/{_action}", _handler, methods=["POST"])

    @app.get("/internal/claims/{claim_id}")
    async def claim_detail(claim_id: str) -> dict[str, object]:
        async with SessionLocal() as session:
            claim = await session.get(MemoryClaim, claim_id)
            if claim is None:
                raise HTTPException(status_code=404, detail="claim not found")
            return {key: value for key, value in claim.__dict__.items() if not key.startswith("_")}

    @app.get("/internal/claims/{claim_id}/evidence")
    async def claim_evidence(claim_id: str) -> list[dict[str, object]]:
        async with SessionLocal() as session:
            rows = (await session.execute(
                text("SELECT * FROM memory_evidence WHERE claim_id = :claim_id ORDER BY heard_at"),
                {"claim_id": claim_id},
            )).mappings().all()
            return [dict(row) for row in rows]

    async def _claim_action(claim_id: str, status: str, source: str) -> dict[str, object]:
        async with SessionLocal() as session:
            claim = await session.get(MemoryClaim, claim_id)
            if claim is None:
                raise HTTPException(status_code=404, detail="claim not found")
            claim.current_status = status
            claim.confirmation_source = source
            claim.version += 1
            memories = (
                await session.execute(
                    text(
                        "SELECT DISTINCT character_id FROM traveler_character_memories "
                        "WHERE claim_id = :claim_id"
                    ),
                    {"claim_id": claim_id},
                )
            ).scalars().all()
            for character_id in memories:
                await owned_runtime.orchestrator.memory.cortex.mark_dirty(
                    session, str(character_id), f"claim:{status.lower()}"
                )
            await session.commit()
            return {key: value for key, value in claim.__dict__.items() if not key.startswith("_")}

    for _slug, _status in (
        ("confirm", "CONFIRMED"),
        ("dispute", "DISPUTED"),
        ("contradict", "CONTRADICTED"),
        ("retract", "RETRACTED"),
    ):
        async def _claim_handler(
            claim_id: str,
            status: str = _status,
        ) -> dict[str, object]:
            return await _claim_action(claim_id, status, "admin")

        app.add_api_route(
            f"/internal/claims/{{claim_id}}/{_slug}",
            _claim_handler,
            methods=["POST"],
        )

    @app.get("/internal/cortex/{character_id}")
    async def cortex_detail(character_id: str) -> dict[str, object]:
        async with SessionLocal() as session:
            snapshot = await owned_runtime.orchestrator.memory.cortex.get_snapshot(session, character_id)
            if snapshot is None:
                raise HTTPException(status_code=404, detail="cortex snapshot not found")
            return {key: value for key, value in snapshot.__dict__.items() if not key.startswith("_")}

    @app.post("/internal/cortex/{character_id}/rebuild")
    async def cortex_rebuild(character_id: str) -> dict[str, object]:
        async with SessionLocal() as session:
            snapshot = await owned_runtime.orchestrator.memory.cortex.rebuild(session, character_id)
            await session.commit()
            return {key: value for key, value in snapshot.__dict__.items() if not key.startswith("_")}

    @app.post("/internal/cortex/{character_id}/reset-generated")
    async def cortex_reset(character_id: str) -> dict[str, object]:
        return await cortex_rebuild(character_id)

    @app.post("/internal/cortex/{character_id}/preview")
    async def cortex_preview(
        character_id: str,
        payload: dict[str, object] = Body(default_factory=dict),
    ) -> dict[str, object]:
        async with SessionLocal() as session:
            raw_route = str(payload.get("route") or "CHAT").upper()
            route = cast(
                MemoryRoute,
                raw_route if raw_route in {"CHAT", "LORE", "MECHANICS", "PLANNER"} else "CHAT",
            )
            raw_entity_keys = payload.get("entity_keys", [])
            entity_keys = [str(item) for item in raw_entity_keys] if isinstance(raw_entity_keys, list) else []

            def payload_int(name: str, default: int) -> int:
                value = payload.get(name)
                if isinstance(value, (int, float, str)):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
                return default

            query = MemoryRecallQuery(
                active_character_id=character_id,
                scene_id=str(payload.get("scene_id")) if payload.get("scene_id") else None,
                text=str(payload.get("query") or ""),
                route=route,
                entity_keys=entity_keys,
            )
            budget = CortexRenderBudget(
                model_id=str(payload.get("model_id") or "runtime"),
                context_length=payload_int("context_length", settings.model_context_length),
                reserved_output_tokens=payload_int("reserved_output_tokens", settings.actor_max_tokens),
            )
            context = await owned_runtime.orchestrator.memory.cortex.get_context(
                session, query=query, budget=budget
            )
            return context.model_dump(mode="json")

    return app

