from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import Float, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import ConversationMessage, RelationshipState, TravelerMemory
from faervell_npc.schemas import MemoryHit, MemoryPerspective, TrustStatus
from faervell_npc.services.embeddings import get_embedder


class MemoryService:
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

    async def archive_message(
        self,
        session: AsyncSession,
        *,
        message_id: str,
        scene_id: str,
        guild_id: str,
        channel_id: str,
        thread_id: str | None,
        speaker_type: str,
        discord_user_id: str | None,
        character_id: str | None,
        profession_mask_id: str,
        content: str,
        created_at: datetime,
        referenced_message_id: str | None = None,
    ) -> None:
        session.add(
            ConversationMessage(
                message_id=message_id,
                scene_id=scene_id,
                guild_id=guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                speaker_type=speaker_type,
                discord_user_id=discord_user_id,
                character_id=character_id,
                profession_mask_id=profession_mask_id,
                content=content,
                created_at=created_at,
                referenced_message_id=referenced_message_id,
            )
        )

    async def extract_local_memories(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        profession_mask_id: str,
        message_id: str,
        content: str,
    ) -> list[TravelerMemory]:
        created: list[TravelerMemory] = []
        perspective = MemoryPerspective.PLAYER_SAID
        memory_type: str | None = None
        importance = 0.45

        if any(pattern.search(content) for pattern in self.PROMISE_PATTERNS):
            memory_type = "PROMISE_OR_DEBT"
            importance = 0.88
        elif any(pattern.search(content) for pattern in self.CLAIM_PATTERNS):
            memory_type = "PLAYER_CLAIM"
            importance = 0.62

        if memory_type:
            statement = f"Персонаж сообщил Страннику: {content.strip()}"
            record = TravelerMemory(
                character_id=character_id,
                observed_under_mask=profession_mask_id,
                memory_type=memory_type,
                perspective=perspective.value,
                statement=statement,
                trust_status=TrustStatus.UNVERIFIED.value,
                importance=importance,
                source_message_ids=[message_id],
                embedding=self.embedder.embed(statement),
            )
            session.add(record)
            created.append(record)
        return created

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        query: str,
        limit: int | None = None,
    ) -> list[MemoryHit]:
        limit = limit or self.settings.max_retrieved_memories
        vector = self.embedder.embed(query)
        semantic = 1.0 - TravelerMemory.embedding.cosine_distance(vector)
        score = cast(0.70 * semantic + 0.20 * TravelerMemory.importance + 0.10, Float).label("score")
        statement = (
            select(TravelerMemory, score)
            .where(
                TravelerMemory.character_id == character_id,
                TravelerMemory.trust_status.notin_([TrustStatus.REJECTED.value, TrustStatus.REDACTED.value]),
            )
            .order_by(score.desc(), TravelerMemory.first_seen_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(statement)).all()
        return [
            MemoryHit(
                id=memory.id,
                statement=memory.statement,
                perspective=MemoryPerspective(memory.perspective),
                trust_status=TrustStatus(memory.trust_status),
                importance=memory.importance,
                source_message_ids=list(memory.source_message_ids or []),
                score=float(computed or 0.0),
                occurred_at=memory.first_seen_at,
            )
            for memory, computed in rows
        ]

    async def recent_messages(
        self,
        session: AsyncSession,
        scene_id: str,
        limit: int | None = None,
    ) -> list[ConversationMessage]:
        limit = limit or self.settings.max_recent_messages
        rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.scene_id == scene_id)
                .order_by(ConversationMessage.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(reversed(rows))

    async def get_or_create_relationship(
        self,
        session: AsyncSession,
        character_id: str,
    ) -> RelationshipState:
        relationship = await session.get(RelationshipState, character_id)
        if relationship is None:
            relationship = RelationshipState(character_id=character_id)
            session.add(relationship)
            await session.flush()
        return relationship

    async def register_interaction(
        self,
        session: AsyncSession,
        relationship: RelationshipState,
        *,
        positive: bool = False,
    ) -> None:
        relationship.familiarity = min(1.0, relationship.familiarity + 0.015)
        if positive:
            relationship.trust = min(1.0, relationship.trust + 0.01)
        relationship.updated_at = datetime.now(UTC)
        relationship.summary = self.relationship_summary(relationship)

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
