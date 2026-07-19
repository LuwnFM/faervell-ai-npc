from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Float, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from faervell_npc.config import get_settings
from faervell_npc.models import KnowledgeChunk, KnowledgeImportRun, SourceRevision
from faervell_npc.schemas import AccessClass, Corpus, DisclosureTier, KnowledgeHit
from faervell_npc.services.embeddings import get_embedder


@dataclass(slots=True)
class KnowledgeDiagnostics:
    documents: int
    chunks: int
    wiki_documents: int
    latest_fetch: datetime | None
    latest_run_status: str | None
    latest_run_errors: list[dict[str, object]]
    healthy: bool
    reason: str


class KnowledgeService:
    _QUERY_STOPWORDS = {
        "кто", "что", "где", "когда", "какой", "какая", "какое", "какие",
        "сейчас", "там", "это", "находится", "находятся", "про", "расскажи",
        "скажи", "известно", "ли", "и", "или", "в", "во", "на", "о", "об",
        "у", "для", "с", "со", "по", "король", "правитель",
    }
    _RUSSIAN_SUFFIXES = (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "иях",
        "ах", "ях", "ов", "ев", "ей", "ой", "ий", "ый", "ая", "яя", "ое",
        "ее", "ую", "юю", "ам", "ям", "ом", "ем", "а", "я", "ы", "и", "у",
        "ю", "е", "о",
    )

    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        corpus: Corpus | None = None,
        limit: int | None = None,
        allowed_access: set[AccessClass] | None = None,
    ) -> list[KnowledgeHit]:
        """Hybrid retrieval with strong title/phrase boosting.

        The old implementation depended mostly on a hashing embedding. That is cheap, but it
        can miss names such as «Королевство Ивелтин». Exact title and lexical matches now win
        before semantic similarity, while the vector score remains useful for paraphrases.
        """
        limit = limit or self.settings.max_retrieved_knowledge
        allowed_access = allowed_access or {
            AccessClass.PUBLIC_CANON,
            AccessClass.PUBLIC_GLOBAL_EVENT,
            AccessClass.PUBLIC_LOCAL_EVENT,
            AccessClass.RUMOR,
            AccessClass.TRAVELER_PRIVATE,
        }
        clean = re.sub(r"\s+", " ", query).strip()
        if not clean:
            return []
        expanded = self._expand_query(clean)
        title_terms = self._query_terms(expanded)
        vector = self.embedder.embed(expanded)
        vector_similarity = 1.0 - KnowledgeChunk.embedding.cosine_distance(vector)
        searchable = func.concat_ws(" ", KnowledgeChunk.title, KnowledgeChunk.section, KnowledgeChunk.content)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", searchable),
            func.websearch_to_tsquery("simple", expanded),
        )
        normalized = clean.casefold()
        exact_title = case(
            (func.lower(KnowledgeChunk.title) == normalized, literal(1.0)),
            (func.lower(KnowledgeChunk.title).contains(normalized), literal(0.82)),
            else_=literal(0.0),
        )
        phrase_match = case(
            (func.lower(KnowledgeChunk.content).contains(normalized), literal(0.38)),
            else_=literal(0.0),
        )
        if title_terms:
            title_token_rank = sum(
                (
                    case(
                        (func.lower(KnowledgeChunk.title).contains(term), literal(1.0)),
                        else_=literal(0.0),
                    )
                    for term in title_terms
                ),
                start=literal(0.0),
            ) / len(title_terms)
        else:
            title_token_rank = literal(0.0)
        combined = cast(
            0.34 * exact_title
            + 0.28 * title_token_rank
            + 0.16 * phrase_match
            + 0.14 * lexical_rank
            + 0.08 * vector_similarity,
            Float,
        ).label("score")

        where: list[ColumnElement[bool]] = [
            KnowledgeChunk.access.in_([value.value for value in allowed_access])
        ]
        if corpus is not None:
            where.append(KnowledgeChunk.corpus == corpus.value)
        statement = (
            select(KnowledgeChunk, SourceRevision.url, SourceRevision.revision, combined)
            .join(SourceRevision, SourceRevision.id == KnowledgeChunk.source_revision_id)
            .where(*where)
            .order_by(combined.desc(), SourceRevision.fetched_at.desc())
            .limit(limit)
        )
        ranked_rows: list[tuple[KnowledgeChunk, str | None, str | None, float]] = []
        try:
            result_rows = (await session.execute(statement)).all()
            ranked_rows = [
                (row[0], row[1], row[2], float(row[3] or 0.0))
                for row in result_rows
            ]
        except Exception:
            # websearch_to_tsquery may reject unusual punctuation. Retrying with a simpler
            # containment query is preferable to returning an empty knowledge packet.
            fallback = (
                select(KnowledgeChunk, SourceRevision.url, SourceRevision.revision)
                .join(SourceRevision, SourceRevision.id == KnowledgeChunk.source_revision_id)
                .where(
                    *where,
                    or_(
                        KnowledgeChunk.title.ilike(f"%{clean}%"),
                        KnowledgeChunk.content.ilike(f"%{clean}%"),
                    ),
                )
                .order_by(SourceRevision.fetched_at.desc())
                .limit(limit)
            )
            fallback_rows = (await session.execute(fallback)).all()
            ranked_rows = [(row[0], row[1], row[2], 0.5) for row in fallback_rows]

        hits: list[KnowledgeHit] = []
        for chunk, url, revision, score in ranked_rows:
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


    @staticmethod
    def _expand_query(query: str) -> str:
        """Add canonical field labels used by Fandom infoboxes without changing intent."""
        lowered = query.casefold()
        additions: list[str] = []
        if re.search(r"\b(?:дата|число|день|год|сезон|время\s+года|календар)\w*", lowered):
            additions.extend(["текущая дата", "календарь", "сезон", "время года", "год", "день"])
        if re.search(r"\b(?:корол|правител|монарх|глава)\w*", lowered):
            additions.extend(["правитель", "нынешний глава", "король", "монарх"])
        if re.search(r"\b(?:где|располож|местонахожд|континент|регион)\w*", lowered):
            additions.extend(["расположение", "география", "континент", "регион"])
        if re.search(r"\b(?:воюет|война|враг|отношени)\w*", lowered):
            additions.extend(["отношения с соседями", "война", "противник"])
        return " ".join([query, *additions]).strip()

    @classmethod
    def _query_terms(cls, query: str) -> list[str]:
        """Extract stable name stems for title matching (Ивелтина -> ивелтин)."""
        raw_terms = re.findall(r"(?iu)[а-яёa-z0-9-]{3,}", query.casefold())
        result: list[str] = []
        for raw in raw_terms:
            if raw in cls._QUERY_STOPWORDS:
                continue
            stem = raw
            if re.fullmatch(r"[а-яё]+", raw):
                for suffix in cls._RUSSIAN_SUFFIXES:
                    if len(stem) - len(suffix) >= 5 and stem.endswith(suffix):
                        stem = stem[: -len(suffix)]
                        break
            if len(stem) >= 4 and stem not in result:
                result.append(stem)
        return result[:8]

    async def search_world(self, session: AsyncSession, query: str, *, limit: int | None = None) -> list[KnowledgeHit]:
        return await self.search(session, query, corpus=None, limit=limit)

    async def diagnostics(self, session: AsyncSession) -> KnowledgeDiagnostics:
        documents = int(
            (await session.execute(select(func.count(func.distinct(SourceRevision.source_id))))).scalar_one()
        )
        chunks = int((await session.execute(select(func.count(KnowledgeChunk.id)))).scalar_one())
        wiki_documents = int(
            (
                await session.execute(
                    select(func.count(func.distinct(SourceRevision.source_id))).where(
                        SourceRevision.source_id.like("faervell_wiki_root:%")
                    )
                )
            ).scalar_one()
        )
        latest_fetch = (
            await session.execute(
                select(func.max(SourceRevision.fetched_at)).where(
                    SourceRevision.source_id.like("faervell_wiki_root:%")
                )
            )
        ).scalar_one_or_none()
        latest_run = (
            await session.execute(
                select(KnowledgeImportRun).order_by(KnowledgeImportRun.started_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        stale_before = datetime.now(UTC) - timedelta(hours=self.settings.knowledge_stale_hours)
        healthy = wiki_documents >= self.settings.knowledge_min_wiki_documents and bool(
            latest_fetch and latest_fetch >= stale_before
        )
        reasons: list[str] = []
        if wiki_documents < self.settings.knowledge_min_wiki_documents:
            reasons.append(
                f"wiki_documents={wiki_documents} < {self.settings.knowledge_min_wiki_documents}"
            )
        if latest_fetch is None:
            reasons.append("no_source_revisions")
        elif latest_fetch < stale_before:
            reasons.append("knowledge_is_stale")
        return KnowledgeDiagnostics(
            documents=documents,
            chunks=chunks,
            wiki_documents=wiki_documents,
            latest_fetch=latest_fetch,
            latest_run_status=latest_run.status if latest_run else None,
            latest_run_errors=list(latest_run.errors or []) if latest_run else [],
            healthy=healthy,
            reason=", ".join(reasons) or "ok",
        )
