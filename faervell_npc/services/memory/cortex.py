from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import TravelerCortexSnapshot

from .cortex_builder import CortexBuilderService
from .cortex_renderer import CortexRenderer
from .recall import MemoryRecallService
from .schemas import CortexContext, CortexRenderBudget, MemoryRecallQuery


class TravelerCortexService:
    def __init__(self, *, builder: CortexBuilderService | None = None, recall: MemoryRecallService | None = None) -> None:
        self.builder = builder or CortexBuilderService()
        self.recall = recall or MemoryRecallService()
        self.renderer = CortexRenderer()

    async def get_snapshot(self, session: AsyncSession, character_id: str, traveler_entity_id: str = "traveler_01") -> TravelerCortexSnapshot | None:
        return (
            await session.execute(
                select(TravelerCortexSnapshot).where(
                    TravelerCortexSnapshot.traveler_entity_id == traveler_entity_id,
                    TravelerCortexSnapshot.character_id == character_id,
                )
            )
        ).scalar_one_or_none()

    async def get_context(
        self,
        session: AsyncSession,
        *,
        query: MemoryRecallQuery,
        budget: CortexRenderBudget,
    ) -> CortexContext:
        snapshot = await self.get_snapshot(session, query.active_character_id, query.traveler_entity_id)
        if snapshot is None or snapshot.dirty:
            snapshot = await self.builder.rebuild_snapshot(
                session, character_id=query.active_character_id,
                reason=(snapshot.dirty_reason if snapshot else "missing"),
                traveler_entity_id=query.traveler_entity_id,
            )
        recalled = await self.recall.recall(session, query)
        testimonies = [item for item in recalled if item.scope_type.value in {"TESTIMONY", "WORLD_TESTIMONY", "RUMOR"}]
        personal = [item for item in recalled if item not in testimonies]
        return self.renderer.render(
            identity_core=snapshot.identity_core,
            personal_memory_digest=snapshot.personal_memory_digest,
            relationship_digest=snapshot.relationship_digest,
            open_threads_digest=snapshot.open_threads_digest,
            testimony_digest=snapshot.testimony_digest,
            shared_world_impressions=snapshot.shared_world_impressions,
            recalled_memories=personal,
            recalled_testimonies=testimonies,
            budget=budget,
            snapshot_version=snapshot.version,
        )

    async def mark_dirty(self, session: AsyncSession, character_id: str, reason: str) -> None:
        snapshot = await self.get_snapshot(session, character_id)
        if snapshot is None:
            snapshot = TravelerCortexSnapshot(character_id=character_id, dirty=True, dirty_reason=reason)
            session.add(snapshot)
        else:
            snapshot.dirty = True
            snapshot.dirty_reason = reason

    async def rebuild(self, session: AsyncSession, character_id: str, reason: str = "manual") -> TravelerCortexSnapshot:
        return await self.builder.rebuild_snapshot(session, character_id=character_id, reason=reason)

    async def reset_generated_sections(self, session: AsyncSession, character_id: str) -> TravelerCortexSnapshot:
        snapshot = await self.rebuild(session, character_id, "reset_generated_sections")
        return snapshot
