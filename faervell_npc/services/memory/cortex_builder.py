from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import (
    RelationshipState,
    TravelerCortexSnapshot,
    TravelerMemory,
    TravelerOpenThread,
)

from .enums import LifecycleStatus, MemoryScope


class CortexBuilderService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def identity_core(self) -> str:
        path = Path(self.settings.behavior_pack_path) / "persona.md"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return "Странник — древний миролюбивый маг Телекинеза и Портализма."

    async def rebuild_snapshot(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        reason: str,
        traveler_entity_id: str = "traveler_01",
    ) -> TravelerCortexSnapshot:
        identity = self.identity_core()
        memories = (
            await session.execute(
                select(TravelerMemory)
                .where(
                    TravelerMemory.traveler_entity_id == traveler_entity_id,
                    TravelerMemory.character_id == character_id,
                    TravelerMemory.lifecycle_status == LifecycleStatus.ACTIVE.value,
                )
                .order_by(TravelerMemory.is_anchor.desc(), TravelerMemory.importance.desc(), TravelerMemory.first_seen_at.asc())
            )
        ).scalars().all()
        relationship = await session.get(RelationshipState, character_id)
        threads = (
            await session.execute(
                select(TravelerOpenThread)
                .where(
                    TravelerOpenThread.traveler_entity_id == traveler_entity_id,
                    TravelerOpenThread.character_id == character_id,
                    TravelerOpenThread.status == "OPEN",
                )
                .order_by(TravelerOpenThread.importance.desc(), TravelerOpenThread.opened_at.asc())
            )
        ).scalars().all()
        personal = [memory for memory in memories if memory.scope_type in {MemoryScope.PERSONAL.value, MemoryScope.SHARED_EVENT.value}]
        testimonies = [memory for memory in memories if memory.scope_type in {MemoryScope.TESTIMONY.value, MemoryScope.WORLD_TESTIMONY.value, MemoryScope.RUMOR.value}]
        personal_digest = self._memory_digest(personal)
        testimony_digest = self._memory_digest(testimonies)
        relationship_digest = relationship.summary if relationship else "незнакомец"
        if relationship:
            relationship_digest = (
                f"{relationship.summary}. Знакомство {relationship.familiarity:.2f}; "
                f"доверие {relationship.trust:.2f}; уважение {relationship.respect:.2f}; "
                f"осторожность {relationship.wariness:.2f}; баланс услуг {relationship.reciprocity_balance}."
            )
        threads_digest = "\n".join(f"[{thread.kind}/{thread.status}] {thread.summary}" for thread in threads)
        source = "|".join(
            [identity, personal_digest, relationship_digest, threads_digest, testimony_digest]
        )
        fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
        snapshot = (
            await session.execute(
                select(TravelerCortexSnapshot).where(
                    TravelerCortexSnapshot.traveler_entity_id == traveler_entity_id,
                    TravelerCortexSnapshot.character_id == character_id,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            snapshot = TravelerCortexSnapshot(
                traveler_entity_id=traveler_entity_id, character_id=character_id,
                version=1,
            )
            session.add(snapshot)
        else:
            snapshot.version += 1
        snapshot.identity_core = identity
        snapshot.personal_memory_digest = personal_digest
        snapshot.relationship_digest = relationship_digest
        snapshot.open_threads_digest = threads_digest
        snapshot.testimony_digest = testimony_digest
        snapshot.shared_world_impressions = self._memory_digest([item for item in testimonies if item.scope_type != MemoryScope.TESTIMONY.value])
        snapshot.source_fingerprint = fingerprint
        snapshot.dirty = False
        snapshot.dirty_reason = reason
        return snapshot

    @staticmethod
    def _memory_digest(memories: list[TravelerMemory]) -> str:
        lines: list[str] = []
        for memory in memories:
            status = memory.trust_status or "UNVERIFIED"
            speaker = f"{memory.speaker_display_name}: " if memory.speaker_display_name else ""
            marker = " [закреплено]" if memory.is_anchor else ""
            lines.append(f"- {speaker}{memory.statement} ({status}){marker}")
        return "\n".join(lines)
