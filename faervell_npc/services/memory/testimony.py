from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .recall import MemoryRecallService
from .schemas import MemoryRecallItem, MemoryRecallQuery, TestimonyCandidate
from .writer import MemoryWriter


class TravelerTestimonyContextService:
    """Route-aware testimony facade kept separate from personal recall."""

    def __init__(self, recall: MemoryRecallService | None = None) -> None:
        self.recall = recall or MemoryRecallService()

    async def get_relevant_testimonies(
        self,
        session: AsyncSession,
        *,
        query: MemoryRecallQuery,
        entity_keys: list[str] | None = None,
        render_budget: int | None = None,
    ) -> list[MemoryRecallItem]:
        del render_budget
        if entity_keys:
            query = query.model_copy(update={"entity_keys": entity_keys})
        return await self.recall.recall_testimonies(session, query)


__all__ = [
    "MemoryRecallService",
    "MemoryWriter",
    "TestimonyCandidate",
    "TravelerTestimonyContextService",
]
