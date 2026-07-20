from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import (
    CharacterProfile,
    ConversationMessage,
    RelationshipState,
    TravelerMemory,
)
from faervell_npc.schemas import MemoryHit, MemoryPerspective, TrustStatus
from faervell_npc.services.embeddings import get_embedder

from .archive import TravelerMemoryArchiveService
from .cortex import TravelerCortexService
from .enums import MemoryScope, MemoryTrust, OpenThreadKind, OpenThreadStatus
from .recall import MemoryRecallService
from .schemas import (
    CortexContext,
    CortexRenderBudget,
    MemoryCandidate,
    MemoryRecallQuery,
    TestimonyCandidate,
)
from .testimony import TravelerTestimonyContextService
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
    OOC_PATTERN = re.compile(r"(?:^|\s)(?:\(\(|//|\[ooc\]|ooc:)", re.I)
    WORLD_TESTIMONY_PATTERN = re.compile(
        r"(?iu)\b(?:говорил\w*|сказал\w*|утвержда\w*|рассказыва\w*|сообщил\w*|"
        r"слышал\w*|сообщал\w*|встречал\w*|видел\w*)\b|"
        r"\b(?:город\w*|страна\w*|государств\w*|дорог\w*|войн\w*|цен\w*|"
        r"монет\w*|корол\w*|рынк\w*|культ\w*|правител\w*|событ\w*)\b"
    )

    def __init__(self, *, lock_manager: object | None = None) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()
        self.writer = MemoryWriter()
        self.recall_service = MemoryRecallService()
        self.cortex = TravelerCortexService(recall=self.recall_service, lock_manager=lock_manager)
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
        guild_id: str | None = None,
        speaker_display_name: str | None = None,
        scene_id: str | None = None,
        location_id: str | None = None,
        referenced_message_id: str | None = None,
    ) -> list[TravelerMemory]:
        if (
            not self.settings.traveler_memory_v2_enabled
            or not self.settings.traveler_memory_v2_write_enabled
            or not self.settings.memory_claims_enabled
            or not self.settings.memory_evidence_enabled
        ):
            return []
        # A reply to an old visit or an OOC note is archived, but cannot create
        # a new memory/testimony record.
        if referenced_message_id or self.OOC_PATTERN.search(content):
            return []
        created: list[TravelerMemory] = []
        memory_type: str | None = None
        importance = 0.45
        scope = MemoryScope.PERSONAL
        trust = MemoryTrust.PLAYER_SAID
        if any(pattern.search(content) for pattern in self.PROMISE_PATTERNS):
            memory_type, importance = "PROMISE_OR_DEBT", 0.88
        elif any(pattern.search(content) for pattern in self.CLAIM_PATTERNS):
            memory_type, importance = "PLAYER_CLAIM", 0.62
        if memory_type is not None and self.settings.memory_personal_enabled:
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
            if result.memory_id:
                memory = await session.get(TravelerMemory, result.memory_id)
                if memory is not None:
                    created.append(memory)
                if memory_type == "PROMISE_OR_DEBT":
                    await self.writer.create_open_thread(
                        session,
                        character_id=character_id,
                        summary=content.strip()[:1200],
                        kind=OpenThreadKind.PROMISE,
                        importance=importance,
                        source_memory_id=result.memory_id,
                    )

        if self.settings.memory_testimony_enabled and self.settings.memory_allow_cross_character_testimony:
            subjects, subject_names = await self._mentioned_profiles(
                session,
                guild_id=guild_id,
                content=content,
                exclude_character_id=character_id,
            )
            if subjects:
                result = await self.writer.record_testimony(
                    session,
                    TestimonyCandidate(
                        owner_character_id=None,
                        scope_type=MemoryScope.TESTIMONY,
                        scene_id=scene_id,
                        location_id=location_id,
                        content=content.strip(),
                        normalized_claim=normalize_text(content),
                        memory_type="TESTIMONY",
                        trust_status=MemoryTrust.OTHER_CHARACTER_SAID,
                        importance=0.58,
                        speaker_character_id=character_id,
                        speaker_display_name=speaker_display_name,
                        subject_character_ids=subjects,
                        source_message_id=message_id,
                        source_id=message_id,
                        why_saved=(
                            "слова персонажа о другом персонаже: "
                            + ", ".join(subject_names)
                        ),
                    ),
                )
                if result.memory_id:
                    memory = await session.get(TravelerMemory, result.memory_id)
                    if memory is not None:
                        created.append(memory)
            elif self.settings.memory_world_testimony_enabled and self.WORLD_TESTIMONY_PATTERN.search(content):
                result = await self.writer.record_testimony(
                    session,
                    TestimonyCandidate(
                        owner_character_id=None,
                        scope_type=MemoryScope.WORLD_TESTIMONY,
                        scene_id=scene_id,
                        location_id=location_id,
                        content=content.strip(),
                        normalized_claim=normalize_text(content),
                        memory_type="WORLD_TESTIMONY",
                        trust_status=MemoryTrust.OTHER_CHARACTER_SAID,
                        importance=0.48,
                        speaker_character_id=character_id,
                        speaker_display_name=speaker_display_name,
                        subject_entity_keys=self._world_entity_keys(content),
                        source_message_id=message_id,
                        source_id=message_id,
                        why_saved="слова персонажа о мире, сохранённые как свидетельство",
                    ),
                )
                if result.memory_id:
                    memory = await session.get(TravelerMemory, result.memory_id)
                    if memory is not None:
                        created.append(memory)
        await session.flush()
        return created

    async def _mentioned_profiles(
        self,
        session: AsyncSession,
        *,
        guild_id: str | None,
        content: str,
        exclude_character_id: str,
    ) -> tuple[list[str], list[str]]:
        if not guild_id:
            return [], []
        profiles = (
            await session.execute(
                select(CharacterProfile).where(
                    CharacterProfile.guild_id == guild_id,
                    CharacterProfile.active.is_(True),
                )
            )
        ).scalars().all()
        subjects: list[str] = []
        names: list[str] = []
        for profile in profiles:
            if profile.id == exclude_character_id:
                continue
            candidates = [profile.canonical_name, *(profile.aliases or [])]
            if any(
                re.search(r"(?iu)(?<!\w)" + re.escape(str(name).strip()) + r"(?!\w)", content)
                for name in candidates
                if str(name).strip()
            ):
                subjects.append(profile.id)
                names.append(profile.canonical_name)
        return subjects, names

    @staticmethod
    def _world_entity_keys(content: str) -> list[str]:
        tokens = re.findall(r"(?iu)[а-яёa-z][а-яёa-z-]{3,}", content)
        stop = {
            "говорил", "сказал", "утверждал", "рассказывал", "сообщил", "слышал",
            "видел", "этот", "этого", "который", "котором", "мне", "тебе", "что",
        }
        return list(dict.fromkeys(token for token in tokens if token.casefold() not in stop))[:12]

    async def record_testimony(self, session: AsyncSession, candidate: TestimonyCandidate):
        if not self.settings.traveler_memory_v2_write_enabled or not self.settings.memory_testimony_enabled:
            return None
        return await self.writer.record_testimony(session, candidate)

    async def resolve_open_thread(
        self,
        session: AsyncSession,
        thread_id: str,
        *,
        status: OpenThreadStatus = OpenThreadStatus.RESOLVED,
        resolution: str = "",
    ):
        return await self.writer.resolve_open_thread(
            session, thread_id, status=status, resolution=resolution
        )

    async def retrieve(self, session: AsyncSession, *, character_id: str, query: str, limit: int | None = None) -> list[MemoryHit]:
        if not self.settings.traveler_memory_v2_read_enabled:
            return []
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
        if not self.settings.traveler_memory_v2_read_enabled:
            return CortexContext()
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
    "TravelerTestimonyContextService",
]
