from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import TravelerMemory
from faervell_npc.services.embeddings import get_embedder

from .config import get_memory_config
from .enums import (
    AttributionMode,
    Confidentiality,
    DisclosureScope,
    LifecycleStatus,
    MemoryScope,
    MemoryTrust,
)
from .ranking import rank_items
from .schemas import MemoryRecallItem, MemoryRecallQuery
from .text import lexical_similarity


class MemoryRecallService:
    def __init__(self) -> None:
        self.embedder = get_embedder()

    async def recall(self, session: AsyncSession, query: MemoryRecallQuery) -> list[MemoryRecallItem]:
        settings = get_settings()
        if not settings.traveler_memory_v2_enabled or not settings.traveler_memory_v2_read_enabled:
            return []
        filters = [
            TravelerMemory.traveler_entity_id == query.traveler_entity_id,
            TravelerMemory.lifecycle_status == LifecycleStatus.ACTIVE.value,
        ]
        # Personal memories are isolated by active character. Testimony/world
        # testimony is intentionally selected through entity/speaker filters.
        personal = TravelerMemory.character_id == query.active_character_id
        if query.allow_testimonies or query.allow_world_rumors:
            shared = TravelerMemory.scope_type.in_([
                MemoryScope.TESTIMONY.value,
                MemoryScope.WORLD_TESTIMONY.value,
                MemoryScope.RUMOR.value,
                MemoryScope.SHARED_EVENT.value,
            ])
            filters.append(or_(personal, shared))
        else:
            filters.append(personal)
        if query.scene_id:
            filters.append(or_(TravelerMemory.source_scene_id == query.scene_id, TravelerMemory.source_scene_id.is_(None)))
        query_vector = self.embedder.embed(query.text) if query.text else None
        statement = select(TravelerMemory).where(*filters)
        is_postgres = False
        try:
            is_postgres = session.get_bind().dialect.name == "postgresql"
        except Exception:
            pass
        if is_postgres and query.text:
            fts = func.to_tsvector(
                "russian", TravelerMemory.normalized_content
            ).op("@@")(func.plainto_tsquery("russian", query.text))
            statement = statement.where(fts)
            if query_vector is not None:
                statement = statement.order_by(TravelerMemory.embedding.cosine_distance(query_vector))
        else:
            statement = statement.order_by(TravelerMemory.importance.desc())
        rows = (await session.execute(statement.limit(get_memory_config().candidate_pool))).scalars().all()
        items: list[MemoryRecallItem] = []
        for memory in rows:
            if memory.scope_type == MemoryScope.TESTIMONY:
                subject_ids = {str(item) for item in (memory.subject_character_ids or [])}
                allowed_ids = {query.active_character_id, *query.query_character_ids}
                if not subject_ids.intersection(allowed_ids) and memory.speaker_character_id != query.active_character_id:
                    continue
            if memory.scope_type == MemoryScope.WORLD_TESTIMONY and query.route != "LORE":
                entity_keys = {str(item).casefold() for item in (memory.subject_entity_keys or [])}
                query_keys = {str(item).casefold() for item in query.entity_keys}
                if entity_keys and query_keys.isdisjoint(entity_keys):
                    continue
            if memory.confidentiality in {Confidentiality.GM_ONLY.value, Confidentiality.REDACTED.value}:
                continue
            if memory.disclosure_scope == DisclosureScope.GM_RESTRICTED.value:
                continue
            if (
                memory.disclosure_scope == DisclosureScope.PRIVATE.value
                and memory.speaker_character_id != query.active_character_id
            ):
                continue
            if (
                memory.attribution_mode == AttributionMode.PRIVATE.value
                and memory.speaker_character_id != query.active_character_id
            ):
                continue
            if memory.scope_type == MemoryScope.TESTIMONY.value and not query.allow_testimonies:
                continue
            if memory.scope_type in {MemoryScope.WORLD_TESTIMONY.value, MemoryScope.RUMOR.value} and not query.allow_world_rumors:
                continue
            semantic = 0.0
            if query_vector is not None and memory.embedding:
                # Hashing embeddings are deterministic; cosine is computed locally
                # to keep recall tests/API independent from an LLM provider.
                dot = sum(a * b for a, b in zip(query_vector, memory.embedding, strict=False))
                left = sum(a * a for a in query_vector) ** 0.5
                right = sum(b * b for b in memory.embedding) ** 0.5
                semantic = max(0.0, dot / (left * right)) if left and right else 0.0
            item = self.to_item(memory, semantic=semantic, query=query)
            if query.text and lexical_similarity(query.text, memory.normalized_content or memory.statement) == 0 and semantic < 0.05 and memory.importance < 0.7:
                continue
            items.append(item)
        return rank_items(items, query.text)

    async def recall_testimonies(self, session: AsyncSession, query: MemoryRecallQuery) -> list[MemoryRecallItem]:
        query = query.model_copy(update={"allow_testimonies": True})
        items = await self.recall(session, query)
        return [item for item in items if item.scope_type in {MemoryScope.TESTIMONY, MemoryScope.WORLD_TESTIMONY, MemoryScope.RUMOR}]

    @staticmethod
    def to_item(memory: TravelerMemory, *, semantic: float, query: MemoryRecallQuery) -> MemoryRecallItem:
        trust = MemoryTrust(memory.trust_status) if memory.trust_status in {item.value for item in MemoryTrust} else MemoryTrust.UNVERIFIED
        mode = AttributionMode(memory.attribution_mode) if memory.attribution_mode in {item.value for item in AttributionMode} else AttributionMode.ATTRIBUTABLE
        scope = MemoryScope(memory.scope_type) if memory.scope_type in {item.value for item in MemoryScope} else MemoryScope.PERSONAL
        instruction = ""
        if scope in {MemoryScope.TESTIMONY, MemoryScope.WORLD_TESTIMONY, MemoryScope.RUMOR}:
            if mode == AttributionMode.ATTRIBUTABLE and memory.speaker_display_name:
                instruction = "Не утверждать как факт; при использовании назвать источник."
            elif mode == AttributionMode.ANONYMOUS:
                instruction = "Сказать только, что Странник это слышал; не раскрывать источник."
            else:
                instruction = "Не раскрывать это свидетельство без разрешения."
        return MemoryRecallItem(
            id=memory.id,
            content=memory.statement,
            normalized_claim=memory.normalized_content,
            scope_type=scope,
            trust_status=trust,
            importance=memory.importance,
            score=semantic,
            speaker_character_id=memory.speaker_character_id,
            speaker_name=memory.speaker_display_name if mode == AttributionMode.ATTRIBUTABLE else None,
            attribution_mode=mode,
            disclosure_scope=DisclosureScope(memory.disclosure_scope) if memory.disclosure_scope in {item.value for item in DisclosureScope} else DisclosureScope.PUBLIC,
            corroboration_count=memory.corroboration_count,
            source_message_id=memory.source_message_id,
            occurred_at=memory.first_seen_at,
            confirmed=trust == MemoryTrust.CONFIRMED,
            actor_instruction=instruction,
        )
