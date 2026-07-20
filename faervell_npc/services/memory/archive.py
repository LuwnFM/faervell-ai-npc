from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import TravelerCortexSnapshot, TravelerMemory

from .enums import LifecycleStatus


class TravelerMemoryArchiveService:
    async def archive(self, session: AsyncSession, memory_id: str, reason: str = "manual") -> TravelerMemory:
        return await self._set(session, memory_id, LifecycleStatus.ARCHIVED, reason)

    async def restore(self, session: AsyncSession, memory_id: str) -> TravelerMemory:
        return await self._set(session, memory_id, LifecycleStatus.ACTIVE, "restore")

    async def redact(self, session: AsyncSession, memory_id: str, reason: str) -> TravelerMemory:
        memory = await self._set(session, memory_id, LifecycleStatus.REDACTED, reason)
        memory.statement = "[REDACTED]"
        memory.normalized_content = "[redacted]"
        memory.metadata_json = {"redaction_reason": reason}
        return memory

    @staticmethod
    async def _set(session: AsyncSession, memory_id: str, status: LifecycleStatus, reason: str) -> TravelerMemory:
        memory = await session.get(TravelerMemory, memory_id)
        if memory is None:
            raise ValueError(f"unknown memory: {memory_id}")
        memory.lifecycle_status = status.value
        memory.updated_at = datetime.now(UTC)
        memory.metadata_json = {**(memory.metadata_json or {}), "lifecycle_reason": reason}
        snapshot = (
            await session.execute(
                select(TravelerCortexSnapshot).where(
                    TravelerCortexSnapshot.traveler_entity_id == memory.traveler_entity_id,
                    TravelerCortexSnapshot.character_id == memory.character_id,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            session.add(
                TravelerCortexSnapshot(
                    traveler_entity_id=memory.traveler_entity_id,
                    character_id=memory.character_id,
                    dirty=True,
                    dirty_reason=f"memory:{status.value.lower()}",
                )
            )
        else:
            snapshot.dirty = True
            snapshot.dirty_reason = f"memory:{status.value.lower()}"
        return memory
