from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import ConversationMessage, RelationshipState, TravelerMemory
from faervell_npc.schemas import MemoryHit, MemoryPerspective, TrustStatus
from faervell_npc.services.embeddings import get_embedder

from .archive import TravelerMemoryArchiveService
from .cortex import TravelerCortexService
from .enums import MemoryScope, MemoryTrust
from .recall import MemoryRecallService
from .schemas import CortexContext, CortexRenderBudget, MemoryCandidate, MemoryRecallQuery, TestimonyCandidate
from .text import normalize_text
from .writer import MemoryWriter


class MemoryService:
    """Compatibility façade used by the orchestration layer during the 1.0 cutover."""

    CLAIM_PATTERNS = [
        re.compile(r"\bя\s+(?:родился|вырос|служил|работал|жил|знаю|умею|был|являюсь)\b", re.I),
        re.compile(r"\bу\s+меня\s+(?:есть|нет|был|была|были)\b", re.I),
        re.compile(r"\bмоя\s+(?:цель|семья|родина|профессия|клятва)\b", re.I),
    ]
    PROMISE_PATTERNS = [
        re.compile(r"\b(?:обещаю|клянусь|принесу|верну|сделаю|доставлю)\b", re.I),
        re.compile(r"\b(?:договорились|буду должен|за мной долг)\b", re.I),
    ]

    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()
        self.writer = MemoryWriter()
        self.recall_service = MemoryRecallService()
        self.cortex = TravelerCortexService(recall=self.recall_service)
        self.archive = TravelerMemoryArchiveService()

    async def archive_message(self, session: AsyncSession, **kwargs: object) -> None:
        message_id = str(kwargs["message_id"])
        if await session.get(ConversationMessage, message_id):
            return
        session.add(ConversationMessage(**kwargs))

    async def extract_local_memories(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        profession_mask_id: str,
        message_id: str,
        content: str,
        scene_id: str | None = None,
        location_id: str | None = None,
        referenced_message_id: str | None = None,
    ) -> list[TravelerMemory]:
        # A reply to an old visit is archived, but cannot create a new memory.
        if referenced_message_id:
            return []
        memory_type: str | None = None
        importance = 0.45
        scope = MemoryScope.PERSONAL
        trust = MemoryTrust.PLAYER_SAID
        if any(pattern.search(content) for pattern in self.PROMISE_PATTERNS):
            memory_type, importance = "PROMISE_OR_DEBT", 0.88
        elif any(pattern.search(content) for pattern in self.CLAIM_PATTERNS):
            memory_type, importance = "PLAYER_CLAIM", 0.62
        if memory_type is None:
            return []
        result = await self.writer.record(
            session,
            MemoryCandidate(
                owner_character_id=character_id,
                scope_type=scope,
                scene_id=scene_id,
                content=content.strip(),
                normalized_claim=normalize_text(content),
                memory_type=memory_type,
                trust_status=trust,
                importance=importance,
                subject_character_ids=[character_id],
                source_message_id=message_id,
                source_id=message_id,
                why_saved="локально выделенное утверждение активного персонажа",
            ),
        )
        await session.flush()
        if not result.memory_id:
            return []
        memory = await session.get(TravelerMemory, result.memory_id)
        return [memory] if memory else []

    async def record_testimony(self, session: AsyncSession, candidate: TestimonyCandidate):
        return await self.writer.record_testimony(session, candidate)

    async def retrieve(self, session: AsyncSession, *, character_id: str, query: str, limit: int | None = None) -> list[MemoryHit]:
        items = await self.recall_service.recall(
            session,
            MemoryRecallQuery(active_character_id=character_id, text=query),
        )
        mapped: list[MemoryHit] = []
        for item in items[: limit or self.settings.max_retrieved_memories]:
            perspective = MemoryPerspective.INFERENCE
            if item.trust_status == MemoryTrust.CONFIRMED:
                perspective = MemoryPerspective.FACT
            elif item.trust_status == MemoryTrust.OBSERVED:
                perspective = MemoryPerspective.OBSERVED
            elif item.scope_type in {MemoryScope.RUMOR, MemoryScope.TESTIMONY, MemoryScope.WORLD_TESTIMONY}:
                perspective = MemoryPerspective.RUMOR
            elif item.trust_status in {MemoryTrust.PLAYER_SAID, MemoryTrust.OTHER_CHARACTER_SAID}:
                perspective = MemoryPerspective.PLAYER_SAID
            trust = TrustStatus.UNVERIFIED
            if item.trust_status == MemoryTrust.CONFIRMED:
                trust = TrustStatus.VERIFIED
            elif item.trust_status == MemoryTrust.DISPUTED:
                trust = TrustStatus.DISPUTED
            mapped.append(MemoryHit(
                id=item.id, statement=item.content, perspective=perspective,
                trust_status=trust, importance=item.importance,
                source_message_ids=[item.source_message_id] if item.source_message_id else [],
                score=item.score, occurred_at=item.occurred_at,
            ))
        return mapped

    async def recent_messages(self, session: AsyncSession, scene_id: str, *, character_id: str | None = None, limit: int | None = None) -> list[ConversationMessage]:
        filters = [ConversationMessage.scene_id == scene_id]
        if character_id:
            filters.append(or_(ConversationMessage.speaker_type == "NPC", ConversationMessage.character_id == character_id))
        rows = (
            await session.execute(
                select(ConversationMessage).where(*filters).order_by(ConversationMessage.created_at.desc()).limit(limit or self.settings.max_recent_messages)
            )
        ).scalars().all()
        return list(reversed(rows))

    async def get_or_create_relationship(self, session: AsyncSession, character_id: str) -> RelationshipState:
        relationship = await session.get(RelationshipState, character_id)
        if relationship is None:
            relationship = RelationshipState(character_id=character_id)
            session.add(relationship)
            await session.flush()
        return relationship

    async def register_interaction(self, session: AsyncSession, relationship: RelationshipState, *, positive: bool = False) -> None:
        relationship.familiarity = min(1.0, relationship.familiarity + 0.015)
        if positive:
            relationship.trust = min(1.0, relationship.trust + 0.01)
        relationship.updated_at = datetime.now(UTC)
        relationship.summary = self.relationship_summary(relationship)
        await self.cortex.mark_dirty(session, relationship.character_id, "relationship:changed")

    async def build_cortex_context(self, session: AsyncSession, *, character_id: str, scene_id: str | None, query: str, route: str = "CHAT") -> CortexContext:
        return await self.cortex.get_context(
            session,
            query=MemoryRecallQuery(active_character_id=character_id, scene_id=scene_id, text=query, route=route),
            budget=CortexRenderBudget(
                model_id="runtime", context_length=getattr(self.settings, "model_context_length", 8192),
                reserved_output_tokens=self.settings.actor_max_tokens,
            ),
        )

    @staticmethod
    def relationship_summary(relationship: RelationshipState) -> str:
        parts: list[str] = []
        if relationship.familiarity < 0.15:
            parts.append("почти незнаком")
        elif relationship.familiarity < 0.55:
            parts.append("уже встречался раньше")
        else:
            parts.append("знаком давно")
        if relationship.trust < 0.25:
            parts.append("относится осторожно")
        elif relationship.trust > 0.7:
            parts.append("заметно доверяет")
        if relationship.irritation > 0.45:
            parts.append("раздражён прошлым поведением")
        if relationship.reciprocity_balance < 0:
            parts.append("помнит невыполненную услугу")
        return ", ".join(parts)


__all__ = [
    "MemoryCandidate", "MemoryRecallQuery", "MemoryService", "MemoryScope", "MemoryTrust",
    "TestimonyCandidate", "TravelerCortexService", "TravelerMemoryArchiveService",
]
