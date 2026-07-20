from __future__ import annotations

from pathlib import Path
from types import MethodType
from typing import Any

import structlog

from faervell_npc.models import RelationshipState
from faervell_npc.schemas import (
    ActorPacket,
    Corpus,
    DisclosureExchange,
    ResponseType,
    SceneContext,
)
from faervell_npc.services.disclosure import DisclosureContext
from faervell_npc.services.retrieval_safety import (
    contamination_reasons,
    extract_relevant_excerpt,
    filter_and_rank,
    has_confident_hit,
    public_contamination_reasons,
    safe_source_fact,
    structured_lore_answer,
)
from faervell_npc.services.synonym_lexicon import SynonymLexicon

HOTFIX_VERSION = "v1.0.1-retrieval-safety-hotfix.2"

_LOG = structlog.get_logger(__name__)


def _lexicon_path() -> Path:
    candidates = (
        Path.cwd() / "data" / "synonyms" / "russian_synonyms.sqlite3",
        Path("/app/data/synonyms/russian_synonyms.sqlite3"),
        Path(__file__).resolve().parents[2]
        / "data"
        / "synonyms"
        / "russian_synonyms.sqlite3",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _citations(hits: list[Any]) -> list[dict[str, str | None]]:
    return [
        {
            "source_id": hit.source_id,
            "title": hit.title,
            "url": hit.url,
            "revision": hit.revision,
        }
        for hit in hits
    ]


def _retrieval_trace(candidates: list[Any], safe: list[Any]) -> dict[str, object]:
    safe_ids = {hit.id for hit in safe}
    rejected: list[dict[str, object]] = []
    for hit in candidates:
        reasons = contamination_reasons(hit)
        if reasons or hit.id not in safe_ids:
            rejected.append(
                {
                    "id": hit.id,
                    "source_id": hit.source_id,
                    "title": hit.title,
                    "score": float(hit.score or 0.0),
                    "reasons": list(reasons) or ["low_relevance"],
                }
            )
    return {
        "safe_hits": [
            {
                "id": hit.id,
                "source_id": hit.source_id,
                "title": hit.title,
                "score": float(hit.score or 0.0),
            }
            for hit in safe
        ],
        "rejected_hits": rejected[:12],
    }


def _deduplicate(hits: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.id in seen:
            continue
        seen.add(hit.id)
        result.append(hit)
    return result


def _quest_like(packet: ActorPacket) -> bool:
    action = packet.action_result or {}
    return bool(
        packet.response_type == ResponseType.QUEST_OFFER
        or packet.quest_summary is not None
        or action.get("quest")
        or action.get("reward_preference_requested")
    )


def install_v101_retrieval_hotfix(runtime: Any) -> None:
    orchestrator = runtime.orchestrator
    if getattr(orchestrator, "_v101_retrieval_hotfix_installed", False):
        return
    orchestrator._v101_retrieval_hotfix_installed = True

    lexicon = SynonymLexicon(_lexicon_path())
    orchestrator.synonym_lexicon = lexicon

    knowledge = orchestrator.knowledge
    original_search = knowledge.search

    async def safe_search(
        self: Any,
        session: Any,
        query: str,
        *,
        corpus: Corpus | None = None,
        limit: int | None = None,
        allowed_access: set[Any] | None = None,
    ) -> list[Any]:
        requested_limit = int(limit or self.settings.max_retrieved_knowledge)
        candidate_limit = max(requested_limit * 8, 40)

        ordinary = await original_search(
            session,
            query,
            corpus=corpus,
            limit=candidate_limit,
            allowed_access=allowed_access,
        )
        safe = filter_and_rank(
            query,
            ordinary,
            corpus=corpus,
            limit=requested_limit,
        )
        if has_confident_hit(query, safe):
            _LOG.info(
                "knowledge_retrieval",
                query=query,
                strategy="ordinary",
                candidate_count=len(ordinary),
                safe_count=len(safe),
                **_retrieval_trace(ordinary, safe),
            )
            return safe

        expansion = lexicon.expand(query)
        if expansion.expanded_query == expansion.canonical_query:
            _LOG.info(
                "knowledge_retrieval",
                query=query,
                strategy="ordinary_no_synonyms",
                candidate_count=len(ordinary),
                safe_count=len(safe),
                **_retrieval_trace(ordinary, safe),
            )
            return safe

        expanded = await original_search(
            session,
            expansion.expanded_query,
            corpus=corpus,
            limit=candidate_limit,
            allowed_access=allowed_access,
        )
        merged = _deduplicate([*ordinary, *expanded])
        reranked = filter_and_rank(
            expansion.canonical_query,
            merged,
            corpus=corpus,
            limit=requested_limit,
        )
        _LOG.info(
            "knowledge_retrieval",
            query=query,
            canonical_query=expansion.canonical_query,
            strategy="synonym_fallback",
            added_terms=list(expansion.added_terms),
            synonym_groups=list(expansion.matched_group_ids),
            candidate_count=len(merged),
            safe_count=len(reranked),
            **_retrieval_trace(merged, reranked),
        )
        return reranked

    knowledge.search = MethodType(safe_search, knowledge)

    original_lore_packet = orchestrator._lore_packet

    async def lore_packet(
        self: Any,
        session: Any,
        query: str,
        context: SceneContext,
    ) -> tuple[ActorPacket, list[dict[str, str | None]]]:
        hits = await self.knowledge.search(
            session,
            query,
            corpus=Corpus.LORE,
            limit=8,
        )
        useful = filter_and_rank(query, hits, corpus=Corpus.LORE, limit=5)
        if not useful:
            await self._create_gap(session, query, context)
            return (
                self.planner.safe_packet(
                    context,
                    "В разрешённых записях Странника нет надёжного ответа.",
                ),
                [],
            )

        relationship = await session.get(RelationshipState, context.character_id)
        trust = relationship.trust if relationship else 0.0
        reciprocity = relationship.reciprocity_balance if relationship else 0
        decisions = [
            self.disclosure.decide(
                hit,
                DisclosureContext(
                    player_raised_topic=True,
                    trust=trust,
                    reciprocity_balance=reciprocity,
                ),
            )
            for hit in useful
        ]

        disclosed_hits: list[Any] = []
        allowed: list[str] = []
        forbidden_labels: list[str] = []
        offer: DisclosureExchange | None = None

        for hit, decision in zip(useful, decisions, strict=True):
            if contamination_reasons(hit):
                continue
            if decision.may_disclose:
                excerpt = extract_relevant_excerpt(query, hit)
                if excerpt:
                    allowed.append(safe_source_fact(query, hit))
                    disclosed_hits.append(hit)
            elif decision.free_summary:
                summary_hit = hit.model_copy(update={"content": decision.free_summary})
                if not contamination_reasons(summary_hit):
                    allowed.append(safe_source_fact(query, summary_hit))
                    disclosed_hits.append(hit)
            forbidden_labels.extend(
                f"withheld:{hit.id}" for _ in decision.withheld_details
            )
            if decision.required_exchange.type != "NONE" and offer is None:
                offer = decision.required_exchange

        if not allowed:
            await self._create_gap(session, query, context)
            return (
                self.planner.safe_packet(
                    context,
                    "В разрешённых записях Странника нет надёжного ответа.",
                ),
                [],
            )

        exact = structured_lore_answer(query, disclosed_hits)
        action_result: dict[str, object] = {
            "retrieval_safety": True,
            "retrieval_hit_ids": [hit.id for hit in disclosed_hits],
        }
        if exact:
            action_result["exact_template_text"] = exact

        return (
            ActorPacket(
                response_type=ResponseType.LORE_ANSWER,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=allowed,
                facts_forbidden=forbidden_labels,
                memories_allowed=[
                    self._memory_for_actor(memory) for memory in context.memories[:2]
                ],
                disclosure_offer=offer,
                action_result=action_result,
                max_length_words=180,
            ),
            _citations(disclosed_hits),
        )

    orchestrator._lore_packet = MethodType(lore_packet, orchestrator)

    async def mechanics_packet(
        self: Any,
        session: Any,
        query: str,
        context: SceneContext,
    ) -> tuple[ActorPacket, list[dict[str, str | None]]]:
        hits = await self.knowledge.search(
            session,
            query,
            corpus=Corpus.MECHANICS,
            limit=8,
        )
        useful = filter_and_rank(query, hits, corpus=Corpus.MECHANICS, limit=5)
        if not useful:
            await self._create_gap(session, query, context)
            return (
                self.planner.safe_packet(
                    context,
                    "Точного правила в загруженных источниках не найдено.",
                ),
                [],
            )
        facts = [safe_source_fact(query, hit) for hit in useful]
        return (
            ActorPacket(
                response_type=ResponseType.MECHANICS_ANSWER,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=facts,
                memories_allowed=[
                    self._memory_for_actor(memory) for memory in context.memories[:2]
                ],
                action_result={
                    "retrieval_safety": True,
                    "retrieval_hit_ids": [hit.id for hit in useful],
                },
                max_length_words=210,
            ),
            _citations(useful),
        )

    orchestrator._mechanics_packet = MethodType(mechanics_packet, orchestrator)

    # Keep a reference for rollback diagnostics even though the replacement is static.
    orchestrator._v101_original_lore_packet = original_lore_packet

    guard = orchestrator.guard
    original_validate = guard.validate

    def validate(self: Any, text: str, packet: ActorPacket) -> Any:
        result = original_validate(text, packet)
        violations = list(result.violations)
        for reason in public_contamination_reasons(text):
            marker = f"retrieval_contamination:{reason}"
            if marker not in violations:
                violations.append(marker)

        if _quest_like(packet):
            lowered = text.casefold()
            forbidden_public = {
                "quest_portal_reference": ("портал", "телепорт"),
                "quest_internal_economy": (
                    "экономическая база",
                    "экономический индекс",
                    "индекс экономики",
                    "отн",
                ),
            }
            for name, terms in forbidden_public.items():
                if any(term in lowered for term in terms):
                    violations.append(name)

        result.violations = list(dict.fromkeys(violations))
        result.passed = not result.violations
        return result

    guard.validate = MethodType(validate, guard)
