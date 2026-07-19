from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import (
    AuditLog,
    ConversationMessage,
    KnowledgeGap,
    RelationshipState,
    SceneConfig,
)
from faervell_npc.schemas import (
    ActorPacket,
    Corpus,
    DisclosureExchange,
    IncomingMessage,
    ProcessResult,
    ResponseType,
    Route,
    RouteDecision,
    SceneContext,
)
from faervell_npc.services.actor import ActorService
from faervell_npc.services.context import SceneContextBuilder
from faervell_npc.services.decision_cache import DecisionCacheService
from faervell_npc.services.disclosure import DisclosureContext, LoreDisclosureEngine
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.knowledge import KnowledgeService
from faervell_npc.services.local_planner import LocalPlanner
from faervell_npc.services.memory import MemoryService
from faervell_npc.services.planner import PlannerService
from faervell_npc.services.router import IntentRouter


class StrangerOrchestrator:
    def __init__(
        self,
        *,
        memory: MemoryService,
        contexts: SceneContextBuilder,
        router: IntentRouter,
        knowledge: KnowledgeService,
        disclosure: LoreDisclosureEngine,
        planner: PlannerService,
        local_planner: LocalPlanner,
        decision_cache: DecisionCacheService,
        actor: ActorService,
        guard: OutputGuard,
    ) -> None:
        self.memory = memory
        self.contexts = contexts
        self.router = router
        self.knowledge = knowledge
        self.disclosure = disclosure
        self.planner = planner
        self.local_planner = local_planner
        self.decision_cache = decision_cache
        self.actor = actor
        self.guard = guard

    async def archive_only(self, session: AsyncSession, incoming: IncomingMessage) -> None:
        scene = await self.contexts.ensure_scene(session, incoming)
        resolution = await self.contexts.resolve_character(session, incoming, scene)
        await self._archive_incoming(session, incoming, scene, resolution.character_id)
        await session.commit()

    async def process(self, session: AsyncSession, incoming: IncomingMessage) -> ProcessResult:
        replay = await self._processed_result(session, incoming.discord_message_id)
        if replay is not None:
            return replay

        scene = await self.contexts.ensure_scene(session, incoming)
        resolution = await self.contexts.resolve_character(session, incoming, scene)
        character_id = resolution.character_id
        character_name = resolution.display_name
        await self._archive_incoming(session, incoming, scene, character_id)

        if resolution.requires_presentation or resolution.requires_name:
            return await self._identity_required_result(
                session,
                incoming=incoming,
                scene=scene,
                character_id=character_id,
                character_name=character_name,
                requires_name=resolution.requires_name,
            )

        context = await self.contexts.build(session, incoming, scene, character_id, character_name)
        route = self.router.decide(incoming.content, has_active_quest=bool(context.active_quests))

        packet: ActorPacket
        planner_model: str | None = None
        citations: list[dict[str, str | None]] = []

        if route.route == Route.MECHANICS:
            packet, citations = await self._mechanics_packet(session, incoming.content, context)
        elif route.route == Route.LORE:
            packet, citations = await self._lore_packet(session, incoming.content, context)
        elif route.route == Route.PLANNER:
            candidate = await self.local_planner.try_handle(
                session,
                player_message=incoming.content,
                context=context,
            )
            if candidate is None:
                candidate = await self.decision_cache.get_approved(
                    session, incoming.content, context
                )
            if candidate is None:
                candidate, planner_model = await self.planner.plan_and_execute(
                    session,
                    player_message=incoming.content,
                    context=context,
                )
                if planner_model is not None:
                    await self.decision_cache.store_candidate(
                        session, incoming.content, context, candidate
                    )
            packet = candidate
        else:
            packet = self._chat_packet(context)

        excluded_models: set[str] = set()
        response = ""
        actor_model: str | None = None
        selection_reason: str | None = None
        guard_result = self.guard.validate("", packet)
        for attempt in range(self.actor.settings.actor_quality_attempts):
            correction = None
            if attempt:
                correction = (
                    "Предыдущий вариант был отброшен автоматической проверкой: "
                    + "; ".join(guard_result.violations)
                    + ". Напиши законченный ответ только на русском языке и не повторяй прежний вариант."
                )
            response, used_model, used_reason = await self.actor.render(
                session,
                packet,
                context,
                correction=correction,
                exclude_models=excluded_models or None,
            )
            if used_model:
                excluded_models.add(used_model)
                actor_model = used_model
            selection_reason = used_reason or selection_reason
            guard_result = self.guard.validate(response, packet)
            if guard_result.passed:
                break
        if not guard_result.passed:
            response = self.actor.fallback(packet, context)
            actor_model = None
            selection_reason = "local_template_actor_quality_guard_failed"
            guard_result = self.guard.validate(response, packet)

        relationship = await self.memory.get_or_create_relationship(session, character_id)
        await self.memory.register_interaction(session, relationship)
        session.add(
            AuditLog(
                actor_type="PLAYER",
                actor_id=character_id,
                action="NPC_RESPONSE_PREPARED",
                scene_id=scene.scene_id,
                message_id=incoming.discord_message_id,
                details={
                    "route": route.model_dump(mode="json"),
                    "actor_model": actor_model,
                    "planner_model": planner_model,
                    "model_selection_reason": selection_reason,
                    "scene_context": context.model_dump(mode="json"),
                    "guard_passed": guard_result.passed,
                    "guard_violations": guard_result.violations,
                    "actor_packet": packet.model_dump(mode="json"),
                    "response": response,
                    "citations": citations,
                },
            )
        )
        await session.commit()

        return ProcessResult(
            route=route,
            response=response,
            actor_packet=packet,
            used_actor_model=actor_model,
            used_planner_model=planner_model,
            planner_escalated=route.route == Route.PLANNER,
            guard_passed=guard_result.passed,
            citations=citations,
            scene_context=context,
            model_selection_reason=selection_reason,
            gm_review_request_id=self._gm_review_id(packet),
        )


    async def _identity_required_result(
        self,
        session: AsyncSession,
        *,
        incoming: IncomingMessage,
        scene: SceneConfig,
        character_id: str,
        character_name: str,
        requires_name: bool,
    ) -> ProcessResult:
        if requires_name:
            response = (
                "Странник задерживает на собеседнике внимательный взгляд. "
                "«Облик я запомнил. Но как мне тебя называть?»"
            )
        else:
            response = (
                "Странник на миг отрывается от своего занятия и оглядывает незнакомца. "
                "«Прежде чем продолжим, назовись — или опиши себя так, чтобы я понимал, "
                "с кем говорю»."
            )
        route = RouteDecision(
            route=Route.CHAT,
            reason="character_presentation_required",
            confidence=1.0,
        )
        packet = ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=scene.scene_id,
            player_name=character_name,
            profession_mask_id=scene.profession_mask_id,
            location_name=scene.location_name,
            facts_forbidden=[
                "Не приписывай собеседнику анкетные сведения до сопоставления личности.",
            ],
            max_length_words=80,
        )
        session.add(
            AuditLog(
                actor_type="PLAYER",
                actor_id=character_id,
                action="NPC_RESPONSE_PREPARED",
                scene_id=scene.scene_id,
                message_id=incoming.discord_message_id,
                details={
                    "route": route.model_dump(mode="json"),
                    "actor_model": None,
                    "planner_model": None,
                    "guard_passed": True,
                    "guard_violations": [],
                    "actor_packet": packet.model_dump(mode="json"),
                    "response": response,
                    "citations": [],
                },
            )
        )
        await session.commit()
        return ProcessResult(
            route=route,
            response=response,
            actor_packet=packet,
            used_actor_model=None,
            used_planner_model=None,
            planner_escalated=False,
            guard_passed=True,
            citations=[],
            scene_context=SceneContext(
                scene_id=scene.scene_id,
                location_id=scene.location_id,
                location_name=scene.location_name,
                category_id=scene.category_id,
                category_name=scene.category_name,
                location_path=scene.location_path or scene.location_name,
                profession_mask_id=scene.profession_mask_id,
                player_name=character_name,
                character_id=character_id,
            ),
        )


    async def _processed_result(
        self, session: AsyncSession, message_id: str
    ) -> ProcessResult | None:
        record = (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.action == "NPC_RESPONSE_PREPARED",
                    AuditLog.message_id == message_id,
                )
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        details = record.details or {}
        try:
            return ProcessResult(
                route=details["route"],
                response=str(details["response"]),
                actor_packet=details["actor_packet"],
                used_actor_model=details.get("actor_model"),
                used_planner_model=details.get("planner_model"),
                planner_escalated=(details.get("route") or {}).get("route") == Route.PLANNER.value,
                guard_passed=bool(details.get("guard_passed", True)),
                citations=list(details.get("citations") or []),
                scene_context=details.get("scene_context"),
                model_selection_reason=details.get("model_selection_reason"),
                gm_review_request_id=self._gm_review_id(
                    ActorPacket.model_validate(details["actor_packet"])
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def record_outgoing(
        self,
        session: AsyncSession,
        *,
        message_id: str,
        guild_id: str,
        channel_id: str,
        content: str,
        created_at: datetime,
    ) -> None:
        if await session.get(ConversationMessage, message_id):
            return
        scene = await session.get(SceneConfig, channel_id)
        if scene is None:
            return
        await self.memory.archive_message(
            session,
            message_id=message_id,
            scene_id=scene.scene_id,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=None,
            speaker_type="NPC",
            discord_user_id=None,
            character_id=None,
            profession_mask_id=scene.profession_mask_id,
            content=content,
            created_at=created_at,
        )
        await session.commit()

    async def _archive_incoming(
        self,
        session: AsyncSession,
        incoming: IncomingMessage,
        scene: SceneConfig,
        character_id: str,
    ) -> None:
        if await session.get(ConversationMessage, incoming.discord_message_id):
            return
        await self.memory.archive_message(
            session,
            message_id=incoming.discord_message_id,
            scene_id=scene.scene_id,
            guild_id=incoming.guild_id,
            channel_id=incoming.channel_id,
            thread_id=incoming.thread_id,
            speaker_type="GM" if incoming.is_gm else "PLAYER",
            discord_user_id=incoming.author_discord_id,
            character_id=character_id,
            profession_mask_id=scene.profession_mask_id,
            content=incoming.content,
            created_at=incoming.created_at,
            referenced_message_id=incoming.referenced_message_id,
        )
        await self.memory.extract_local_memories(
            session,
            character_id=character_id,
            profession_mask_id=scene.profession_mask_id,
            message_id=incoming.discord_message_id,
            content=incoming.content,
        )
        await session.flush()

    def _chat_packet(self, context: SceneContext) -> ActorPacket:
        memories = [self._memory_for_actor(memory) for memory in context.memories[:3]]
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[],
            memories_allowed=memories,
            max_length_words=180,
        )

    async def _mechanics_packet(
        self,
        session: AsyncSession,
        query: str,
        context: SceneContext,
    ) -> tuple[ActorPacket, list[dict[str, str | None]]]:
        hits = await self.knowledge.search_world(session, query)
        useful = [hit for hit in hits if hit.corpus == Corpus.MECHANICS and hit.score >= 0.08][:5]
        if not useful:
            await self._create_gap(session, query, context)
            return self.planner.safe_packet(context, "Точного правила в загруженных источниках не найдено."), []

        facts = [self._source_fact(hit.content, hit.title) for hit in useful]
        citations = [
            {"source_id": hit.source_id, "title": hit.title, "url": hit.url, "revision": hit.revision}
            for hit in useful
        ]
        return (
            ActorPacket(
                response_type=ResponseType.MECHANICS_ANSWER,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=facts,
                memories_allowed=[self._memory_for_actor(memory) for memory in context.memories[:2]],
                max_length_words=230,
                ooc_note="Источники приложены сервером отдельно.",
            ),
            citations,
        )

    async def _lore_packet(
        self,
        session: AsyncSession,
        query: str,
        context: SceneContext,
    ) -> tuple[ActorPacket, list[dict[str, str | None]]]:
        hits = await self.knowledge.search_world(session, query)
        useful = [hit for hit in hits if hit.score >= 0.08][:5]
        if not useful:
            await self._create_gap(session, query, context)
            return self.planner.safe_packet(context, "В разрешённых знаниях Странника нет надёжного ответа."), []

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
        allowed: list[str] = []
        forbidden_labels: list[str] = []
        offer: DisclosureExchange | None = None
        for hit, decision in zip(useful, decisions, strict=True):
            if decision.may_disclose:
                allowed.append(self._source_fact(hit.content, hit.title))
            elif decision.free_summary:
                allowed.append(self._source_fact(decision.free_summary, hit.title))
            forbidden_labels.extend(f"withheld:{hit.id}" for _ in decision.withheld_details)
            if decision.required_exchange.type != "NONE" and offer is None:
                offer = decision.required_exchange

        citations = [
            {"source_id": hit.source_id, "title": hit.title, "url": hit.url, "revision": hit.revision}
            for hit in useful
            if any(
                dec.knowledge_id == hit.id and (dec.may_disclose or dec.free_summary)
                for dec in decisions
            )
        ]
        return (
            ActorPacket(
                response_type=ResponseType.LORE_ANSWER,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=allowed,
                facts_forbidden=forbidden_labels,
                memories_allowed=[self._memory_for_actor(memory) for memory in context.memories[:2]],
                disclosure_offer=offer,
                max_length_words=220,
            ),
            citations,
        )

    async def regenerate(
        self,
        session: AsyncSession,
        *,
        packet: ActorPacket,
        context: SceneContext,
        excluded_models: set[str],
    ) -> tuple[str, str | None, str | None]:
        """Regenerate the literary surface only; facts and state remain unchanged."""
        excluded = set(excluded_models)
        response = ""
        model: str | None = None
        reason: str | None = None
        guard = self.guard.validate("", packet)
        for _attempt in range(self.actor.settings.actor_quality_attempts):
            response, used_model, used_reason = await self.actor.render(
                session,
                packet,
                context,
                correction=(
                    "Сделай новый вариант заметно иначе, без повторения прежних действий и "
                    "формулировок. Ответ должен быть завершённым и полностью русскоязычным."
                ),
                free_only=True,
                exclude_models=excluded,
            )
            if used_model:
                excluded.add(used_model)
                model = used_model
            reason = used_reason or reason
            guard = self.guard.validate(response, packet)
            if guard.passed:
                return response, model, reason
        response = self.actor.fallback(packet, context)
        return response, None, "local_template_regeneration_guard_failed"

    @staticmethod
    def _gm_review_id(packet: ActorPacket) -> str | None:
        value = packet.action_result.get("gm_review_request_id")
        return str(value) if value else None

    async def _create_gap(self, session: AsyncSession, question: str, context: SceneContext) -> None:
        session.add(
            KnowledgeGap(
                question=question,
                scene_id=context.scene_id,
                character_id=context.character_id,
                profession_mask_id=context.profession_mask_id,
                evidence=[],
            )
        )

    @staticmethod
    def _memory_for_actor(memory: object) -> str:
        perspective = getattr(memory, "perspective", "PLAYER_SAID")
        statement = getattr(memory, "statement", "")
        return f"[{perspective}] {statement}"

    @staticmethod
    def _source_fact(content: str, title: str) -> str:
        return f"По источнику «{title}»: {content.strip()}"
