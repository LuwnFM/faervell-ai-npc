from __future__ import annotations

from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import KnowledgeChunk, SourceRevision
from faervell_npc.schemas import AccessClass, Corpus, DisclosureTier, KnowledgeHit
from faervell_npc.services.embeddings import get_embedder


class KnowledgeService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        corpus: Corpus,
        limit: int | None = None,
        allowed_access: set[AccessClass] | None = None,
    ) -> list[KnowledgeHit]:
        limit = limit or self.settings.max_retrieved_knowledge
        allowed_access = allowed_access or {
            AccessClass.PUBLIC_CANON,
            AccessClass.PUBLIC_GLOBAL_EVENT,
            AccessClass.PUBLIC_LOCAL_EVENT,
            AccessClass.RUMOR,
            AccessClass.TRAVELER_PRIVATE,
        }
        vector = self.embedder.embed(query)
        vector_similarity = 1.0 - KnowledgeChunk.embedding.cosine_distance(vector)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", KnowledgeChunk.content),
            func.plainto_tsquery("simple", query),
        )
        combined = cast(0.78 * vector_similarity + 0.22 * lexical_rank, Float).label("score")

        statement = (
            select(KnowledgeChunk, SourceRevision.url, SourceRevision.revision, combined)
            .join(SourceRevision, SourceRevision.id == KnowledgeChunk.source_revision_id)
            .where(
                KnowledgeChunk.corpus == corpus.value,
                KnowledgeChunk.access.in_([value.value for value in allowed_access]),
            )
            .order_by(combined.desc())
            .limit(limit)
        )
        rows = (await session.execute(statement)).all()
        hits: list[KnowledgeHit] = []
        for chunk, url, revision, score in rows:
            hits.append(
                KnowledgeHit(
                    id=chunk.id,
                    source_id=chunk.source_id,
                    title=chunk.title,
                    content=chunk.content,
                    corpus=Corpus(chunk.corpus),
                    access=AccessClass(chunk.access),
                    disclosure_tier=DisclosureTier(chunk.disclosure_tier),
                    disclosure_modes=list(chunk.disclosure_modes or []),
                    score=float(score or 0.0),
                    url=url,
                    revision=revision,
                    metadata=dict(chunk.metadata_json or {}),
                )
            )
        return hits
