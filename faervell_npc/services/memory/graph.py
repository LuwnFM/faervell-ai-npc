from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import MemoryRelation

from .enums import MemoryRelationType


class MemoryGraphService:
    """PostgreSQL-backed relation store; disabled by default and never invents facts."""

    async def link(
        self,
        session: AsyncSession,
        source_memory_id: str,
        target_memory_id: str,
        relation_type: MemoryRelationType,
        strength: float = 1.0,
        source: str = "LOCAL",
    ) -> MemoryRelation:
        relation = (
            await session.execute(
                select(MemoryRelation).where(
                    MemoryRelation.source_memory_id == source_memory_id,
                    MemoryRelation.target_memory_id == target_memory_id,
                    MemoryRelation.relation_type == relation_type.value,
                )
            )
        ).scalar_one_or_none()
        if relation is None:
            relation = MemoryRelation(
                source_memory_id=source_memory_id, target_memory_id=target_memory_id,
                relation_type=relation_type.value, strength=strength, source=source,
            )
            session.add(relation)
        return relation
