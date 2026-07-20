from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Any

from sqlalchemy import select

from faervell_npc.models import (
    ConversationMessage,
    GMReviewRequest,
    KnowledgeGap,
    ModelCall,
    RelationshipState,
)
from faervell_npc.schemas import ActorPacket, DisclosureExchange, ResponseType
from faervell_npc.services.disclosure import DisclosureContext
from faervell_npc.services.guard import GuardResult
from faervell_npc.services.v080_grounding import (
    foreign_script_violations,
    is_noise_gap,
    latest_world_clock_fact,
    missing_facets,
    missing_fact_sentence,
    normalize_gap_key,
    normalize_gap_question,
    pending_quest_violations,
    repair_actor_packet,
    requested_facets,
    safe_evidence,
    scrub_model_error,
    ungrounded_lore_claim_violations,
    ungrounded_lore_violations,
)

_KNOWLEDGE_SERVICE: Any | None = None


def install_v080_runtime(
    *,
    planner: Any,
    orchestrator: Any,
    actor: Any,
    guard: Any,
    rules: Any,
    characters: Any,
    local_planner: Any,
) -> None:
    """Attach v0.8 policies to the v0.7.4 services.

    The patch is deliberately installed from ``build_runtime``. This keeps the release
    reversible and avoids duplicating the whole planner/orchestrator implementation.
    """
    global _KNOWLEDGE_SERVICE
    if getattr(orchestrator, "_v080_installed", False):
        return
    orchestrator._v080_installed = True
    _KNOWLEDGE_SERVICE = orchestrator.knowledge

    _install_partial_lore(orchestrator)
    _install_planner_repair(planner, orchestrator)
    _install_output_grounding(guard)
    _install_mask_neutral_quests(rules)
    _install_local_quest_policy(local_planner)
    _install_character_presentation(characters)
    _install_behavior_scan()
    _install_actor_policy(actor)


def _install_planner_repair(planner: Any, orchestrator: Any) -> None:
    original = planner._finalize

    async def finalize(
        self: Any,
        session: Any,
        player_message: str,
        context: Any,
        plan: Any,
        tool_results: list[dict[str, object]],
    ) -> ActorPacket:
        packet = await original(session, player_message, context, plan, tool_results)
        packet = repair_actor_packet(
            packet,
            player_message=player_message,
            tool_results=tool_results,
            context=context,
        )
        facets = requested_facets(player_message)
        # The weather MVP exposes the real server date. It is not the Faervell calendar.
        # A dated trusted world-news revision is the canonical source for current world time.
        if any(item in facets for item in ("year", "date")):
            clock = await latest_world_clock_fact(session)
            if clock:
                packet.facts_allowed = [
                    fact
                    for fact in packet.facts_allowed
                    if not re.search(
                        r"(?iu)(?:текущая\s+дата\s+мира|\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b)",
                        fact,
                    )
                    and not fact.startswith("В доступных подтверждённых сведениях не указан")
                ]
                packet.facts_allowed.append(clock)

        missing = missing_facets(player_message, packet.facts_allowed)
        packet.facts_allowed = [
            fact
            for fact in packet.facts_allowed
            if not fact.startswith("В доступных подтверждённых сведениях не указан")
        ]
        if missing:
            from faervell_npc.services.v080_grounding import extract_tool_evidence

            gap_info = await orchestrator._create_gap(
                session,
                question=player_message,
                context=context,
                missing=missing,
                evidence=extract_tool_evidence(tool_results),
            )
            if gap_info:
                packet.action_result.update(gap_info)
            packet.action_result["missing_facets"] = missing
            packet.facts_allowed.append(missing_fact_sentence(missing))
        packet.facts_allowed = list(dict.fromkeys(packet.facts_allowed))[:10]
        return packet

    planner._finalize = MethodType(finalize, planner)

    original_pending = planner._pending_quest_packet

    def pending_packet(context: Any, pending: dict[str, object]) -> ActorPacket:
        packet = original_pending(context, pending)
        packet.required_mentions = ["нужно уточнить"]
        packet.quest_summary = None
        packet.facts_allowed = [
            "Работа найдётся, но сперва нужно уточнить конкретную цель, условия и плату.",
            "Пока поручение не подтверждено, отправляться или забирать предметы не нужно.",
        ]
        return packet

    # Static method access on an instance returns the function itself.
    planner._pending_quest_packet = pending_packet


def _install_partial_lore(orchestrator: Any) -> None:
    async def create_gap(
        self: Any,
        session: Any,
        question: str,
        context: Any,
        *,
        missing: list[str] | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, str] | None:
        if is_noise_gap(question):
            return None
        normalized = normalize_gap_question(question, missing)
        key = normalize_gap_key(normalized)
        filtered_evidence = safe_evidence(evidence or [])
        pending = (
            await session.execute(
                select(KnowledgeGap).where(KnowledgeGap.status == "PENDING")
            )
        ).scalars().all()
        for existing in pending:
            if normalize_gap_key(existing.question) == key:
                existing_review = (
                    await session.execute(
                        select(GMReviewRequest).where(
                            GMReviewRequest.request_type == "KNOWLEDGE_GAP",
                            GMReviewRequest.status == "PENDING",
                        )
                    )
                ).scalars().all()
                for review in existing_review:
                    if str((review.payload or {}).get("knowledge_gap_id")) == existing.id:
                        return {
                            "knowledge_gap_id": existing.id,
                            "gm_review_request_id": review.id,
                        }
                break
        else:
            existing = None

        if existing is not None:
            gap = existing
        else:
            gap = KnowledgeGap(
                question=normalized,
                scene_id=context.scene_id,
                character_id=context.character_id,
                profession_mask_id=context.profession_mask_id,
                evidence=filtered_evidence,
            )
            session.add(gap)
            await session.flush()

        latest_player = (
            await session.execute(
                select(
                    ConversationMessage.discord_user_id,
                    ConversationMessage.guild_id,
                    ConversationMessage.channel_id,
                )
                .where(
                    ConversationMessage.scene_id == context.scene_id,
                    ConversationMessage.character_id == context.character_id,
                    ConversationMessage.speaker_type.in_(["PLAYER", "GM"]),
                )
                .order_by(ConversationMessage.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
        player_id = str(latest_player[0]) if latest_player and latest_player[0] else None
        guild_id = str(latest_player[1]) if latest_player else "unknown"
        channel_id = str(latest_player[2]) if latest_player else context.scene_id
        review = GMReviewRequest(
            guild_id=guild_id,
            scene_id=context.scene_id,
            channel_id=channel_id,
            player_discord_user_id=player_id,
            character_id=context.character_id,
            request_type="KNOWLEDGE_GAP",
            reason="knowledge_confirmation_required",
            payload={
                "knowledge_gap_id": gap.id,
                "question": normalized,
                "missing_facets": list(missing or []),
                "evidence": filtered_evidence,
            },
        )
        session.add(review)
        await session.flush()
        return {"knowledge_gap_id": gap.id, "gm_review_request_id": review.id}

    async def lore_packet(
        self: Any,
        session: Any,
        query: str,
        context: Any,
    ) -> tuple[ActorPacket, list[dict[str, str | None]]]:
        hits = await self.knowledge.search_world(session, query, limit=12)
        temporal = bool(
            re.search(r"(?iu)\b(?:сейчас|нынешн|текущ|последн|сегодня|на\s+данный\s+момент)\w*", query)
        )
        ranked = sorted(
            hits,
            key=lambda hit: (
                1 if temporal and str(hit.source_id).startswith("discord_world_news:") else 0,
                float(hit.score),
            ),
            reverse=True,
        )
        useful = [hit for hit in ranked if hit.score >= 0.08][:8]

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
        evidence: list[dict[str, Any]] = []
        citations: list[dict[str, str | None]] = []
        for hit, decision in zip(useful, decisions, strict=True):
            disclosed = ""
            if decision.may_disclose:
                disclosed = hit.content
            elif decision.free_summary:
                disclosed = decision.free_summary
            if disclosed:
                if str(hit.source_id).startswith("discord_world_news:"):
                    date_label = str((hit.metadata or {}).get("world_date_label") or "").strip()
                    prefix = (
                        f"Последнее подтверждённое состояние мира на {date_label}: "
                        if date_label
                        else "Последнее подтверждённое состояние мира: "
                    )
                    allowed.append(prefix + disclosed.strip())
                else:
                    allowed.append(self._source_fact(disclosed, hit.title))
                citations.append(
                    {
                        "source_id": hit.source_id,
                        "title": hit.title,
                        "url": hit.url,
                        "revision": hit.revision,
                    }
                )
            forbidden_labels.extend(f"withheld:{hit.id}" for _ in decision.withheld_details)
            if decision.required_exchange.type != "NONE" and offer is None:
                offer = decision.required_exchange
            evidence.append(
                {
                    "id": hit.id,
                    "source_id": hit.source_id,
                    "title": hit.title,
                    "url": hit.url,
                    "revision": hit.revision,
                    "score": hit.score,
                }
            )

        facets = requested_facets(query)
        if any(item in facets for item in ("year", "date", "time")):
            clock = await latest_world_clock_fact(session)
            if clock and clock not in allowed:
                allowed.append(clock)

        missing = missing_facets(query, allowed)
        action_result: dict[str, Any] = {"player_said": query}
        if missing or not allowed:
            gap_info = await create_gap(
                self,
                session,
                question=query,
                context=context,
                missing=missing or facets,
                evidence=evidence,
            )
            if gap_info:
                action_result.update(gap_info)
            action_result["missing_facets"] = missing or facets

        unknown = missing_fact_sentence(missing)
        if unknown:
            allowed.append(unknown)

        # A partial answer remains a lore answer. SAFE_UNKNOWN is reserved for a query
        # for which no disclosable fact at all was found.
        response_type = ResponseType.LORE_ANSWER if allowed and any(
            not item.startswith("В доступных подтверждённых сведениях") for item in allowed
        ) else ResponseType.SAFE_UNKNOWN
        if response_type == ResponseType.SAFE_UNKNOWN and not allowed:
            allowed = ["В разрешённых знаниях Странника нет надёжного ответа по этой теме."]

        return (
            ActorPacket(
                response_type=response_type,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=allowed[:10],
                facts_forbidden=forbidden_labels,
                memories_allowed=[],
                disclosure_offer=offer,
                action_result=action_result,
                max_length_words=240,
            ),
            citations,
        )

    orchestrator._create_gap = MethodType(create_gap, orchestrator)
    orchestrator._lore_packet = MethodType(lore_packet, orchestrator)


def _install_output_grounding(guard: Any) -> None:
    original = guard.validate

    def validate(self: Any, text: str, packet: ActorPacket) -> GuardResult:
        result = original(text, packet)
        additions = [
            *foreign_script_violations(text),
            *ungrounded_lore_violations(text, packet),
            *ungrounded_lore_claim_violations(text, packet),
            *pending_quest_violations(text, packet),
        ]
        for violation in additions:
            if violation not in result.violations:
                result.violations.append(violation)
        result.passed = not result.violations
        return result

    guard.validate = MethodType(validate, guard)


def _install_mask_neutral_quests(rules: Any) -> None:
    original = rules.validate_quest

    def validate_quest(self: Any, quest: Any, profession_mask_id: str) -> Any:
        result = original(quest, profession_mask_id)
        result.errors = [
            error for error in result.errors if error != "profession_mask_cannot_issue_template"
        ]
        result.valid = not result.errors

        # Delivery inside the current location is allowed. Only explicitly vague local
        # destinations are escalated for clarification instead of being announced as ready.
        if str(getattr(quest, "template_id", "")) == "DELIVER_ITEM":
            description = str(getattr(quest, "description", "") or "")
            targets = [str(getattr(obj, "target_id", "") or "") for obj in quest.objectives]
            vague = bool(
                re.search(
                    r"(?iu)\b(?:в\s+безопасное\s+место|куда[- ]?нибудь|в\s+подходящее\s+место|в\s+этой\s+же\s+локации)\b",
                    description,
                )
            )
            has_recipient = bool(
                any(targets)
                or re.search(r"(?iu)\b(?:передать|получател|адресат|кому|страж|мастер|писар|торговц|курьер)\w*\b", description)
            )
            if vague and not has_recipient:
                result.requires_gm_approval = True
        return result

    rules.validate_quest = MethodType(validate_quest, rules)


def _install_local_quest_policy(local_planner: Any) -> None:
    original_build = local_planner._build_quest

    def build_quest(*, player_message: str, destination: str, evidence_ids: list[str]) -> Any:
        quest = original_build(
            player_message=player_message,
            destination=destination,
            evidence_ids=evidence_ids,
        )
        if str(getattr(quest, "template_id", "")) == "DELIVER_ITEM":
            quest.description = (
                f"Получить у Странника запечатанный дорожный пакет, передать назначенному "
                f"получателю в {quest.location_name or destination} и вернуться с подтверждением."
            )
        return quest

    local_planner._build_quest = build_quest
    original_grounded = local_planner._grounded_local_quest

    async def grounded_local_quest(
        self: Any,
        session: Any,
        *,
        player_message: str,
        context: Any,
    ) -> ActorPacket:
        packet = await original_grounded(
            session, player_message=player_message, context=context
        )
        status = str((packet.action_result or {}).get("status") or "").upper()
        has_review = bool((packet.action_result or {}).get("gm_review_request_id"))
        if status in {"PENDING", "PENDING_GM", "PENDING_REVIEW"} or has_review:
            packet.response_type = ResponseType.DIALOGUE
            packet.quest_summary = None
            packet.required_mentions = ["нужно уточнить"]
            packet.facts_allowed = [
                "Работа найдётся, но сперва нужно уточнить конкретную цель, условия и плату.",
                "Пока поручение не подтверждено, отправляться или забирать предметы не нужно.",
            ]
            packet.max_length_words = min(packet.max_length_words, 120)
        return packet

    local_planner._grounded_local_quest = MethodType(grounded_local_quest, local_planner)


def _install_character_presentation(characters: Any) -> None:
    cls = type(characters)
    if getattr(cls, "_v080_presentation_installed", False):
        return
    cls._v080_presentation_installed = True
    original = cls.extract_presentation

    def extract_presentation(text: str) -> Any:
        result = original(text)
        if result.is_presentation:
            return result
        match = re.match(
            r"(?u)^\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’\-]{1,40}(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’\-]{1,40}){0,3})\s*[—–-]\s*[^\n]{1,80}\s*$",
            text,
        )
        if not match:
            return result
        name = cls.clean_presented_name(match.group(1))
        return type(result)(presented_name=name, has_appearance=False, is_presentation=bool(name))

    cls.extract_presentation = staticmethod(extract_presentation)


def _install_behavior_scan() -> None:
    from faervell_npc.services.behavior import BehaviorManager

    async def scan(self: Any, session: Any, days: int = 30) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=days)
        raw_gaps = (
            await session.execute(
                select(KnowledgeGap).where(
                    KnowledgeGap.created_at >= since,
                    KnowledgeGap.status == "PENDING",
                )
            )
        ).scalars().all()
        existing_reviews = (
            await session.execute(
                select(GMReviewRequest).where(
                    GMReviewRequest.request_type == "KNOWLEDGE_GAP",
                    GMReviewRequest.status == "PENDING",
                )
            )
        ).scalars().all()
        reviewed_gap_ids = {
            str((review.payload or {}).get("knowledge_gap_id") or "")
            for review in existing_reviews
        }
        gm_reviews_backfilled = 0
        gap_by_id = {gap.id: gap for gap in raw_gaps}
        backfilled_keys: set[str] = {
            normalize_gap_key(normalize_gap_question(gap_by_id[gap_id].question))
            for gap_id in reviewed_gap_ids
            if gap_id in gap_by_id
        }
        for gap in raw_gaps:
            if gap.id in reviewed_gap_ids or is_noise_gap(gap.question):
                continue
            key = normalize_gap_key(normalize_gap_question(gap.question))
            if not key or key in backfilled_keys:
                continue
            latest_player = (
                await session.execute(
                    select(
                        ConversationMessage.discord_user_id,
                        ConversationMessage.guild_id,
                        ConversationMessage.channel_id,
                    )
                    .where(
                        ConversationMessage.scene_id == gap.scene_id,
                        ConversationMessage.character_id == gap.character_id,
                        ConversationMessage.speaker_type.in_(["PLAYER", "GM"]),
                    )
                    .order_by(ConversationMessage.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
            if latest_player is None:
                continue
            review = GMReviewRequest(
                guild_id=str(latest_player[1]),
                scene_id=gap.scene_id,
                channel_id=str(latest_player[2]),
                player_discord_user_id=str(latest_player[0]) if latest_player[0] else None,
                character_id=gap.character_id,
                request_type="KNOWLEDGE_GAP",
                reason="knowledge_gap_backfilled_by_behavior_scan",
                payload={
                    "knowledge_gap_id": gap.id,
                    "question": normalize_gap_question(gap.question),
                    "backfilled": True,
                },
            )
            session.add(review)
            reviewed_gap_ids.add(gap.id)
            backfilled_keys.add(key)
            gm_reviews_backfilled += 1
        if gm_reviews_backfilled:
            await session.flush()

        grouped: dict[str, dict[str, Any]] = {}
        for gap in raw_gaps:
            if is_noise_gap(gap.question):
                continue
            normalized = normalize_gap_question(gap.question)
            key = normalize_gap_key(normalized)
            if not key:
                continue
            record = grouped.setdefault(
                key,
                {
                    "question": normalized,
                    "count": 0,
                    "variants": [],
                    "gap_ids": [],
                    "first_created_at": gap.created_at,
                },
            )
            record["count"] += 1
            record["gap_ids"].append(gap.id)
            if gap.question not in record["variants"] and len(record["variants"]) < 5:
                record["variants"].append(gap.question)
            if gap.created_at < record["first_created_at"]:
                record["first_created_at"] = gap.created_at

        # Re-check the current index. A gap that is already fully answered by newly imported
        # canon should not keep polluting behavior_scan.
        unresolved: list[dict[str, Any]] = []
        auto_resolved_gaps = 0
        auto_closed_reviews = 0
        knowledge = _KNOWLEDGE_SERVICE
        for record in sorted(grouped.values(), key=lambda item: item["count"], reverse=True)[:100]:
            resolved_now = False
            if knowledge is not None:
                try:
                    hits = await knowledge.search_world(session, record["question"], limit=8)
                    facts = [hit.content for hit in hits if hit.score >= 0.08]
                    resolved_now = bool(facts) and not missing_facets(record["question"], facts)
                except Exception:
                    resolved_now = False
            if resolved_now:
                resolved_ids = set(record.get("gap_ids") or [])
                for gap_id in resolved_ids:
                    gap = gap_by_id.get(gap_id)
                    if gap is not None and gap.status == "PENDING":
                        gap.status = "RESOLVED"
                        auto_resolved_gaps += 1
                for review in existing_reviews:
                    gap_id = str((review.payload or {}).get("knowledge_gap_id") or "")
                    if gap_id in resolved_ids and review.status == "PENDING":
                        review.status = "REJECTED"
                        review.decision_note = "Пробел автоматически закрыт: ответ уже найден в актуальном индексе"
                        review.decided_at = datetime.now(UTC)
                        auto_closed_reviews += 1
                continue
            record["first_created_at"] = record["first_created_at"].isoformat()
            record.pop("gap_ids", None)
            unresolved.append(record)

        errors = (
            await session.execute(
                select(ModelCall.kind, ModelCall.model, ModelCall.error, ModelCall.created_at)
                .where(ModelCall.created_at >= since, ModelCall.success.is_(False))
                .order_by(ModelCall.created_at.desc())
                .limit(100)
            )
        ).all()
        error_counts: Counter[tuple[str, str, str]] = Counter()
        latest: dict[tuple[str, str, str], datetime] = {}
        for kind, model, error, created_at in errors:
            clean = scrub_model_error(str(error or ""))
            error_key = (str(kind), str(model), clean)
            error_counts[error_key] += 1
            latest[error_key] = max(created_at, latest.get(error_key, created_at))

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "days": days,
            "pending_knowledge_gaps": unresolved,
            "gm_reviews_backfilled": gm_reviews_backfilled,
            "auto_resolved_gaps": auto_resolved_gaps,
            "auto_closed_reviews": auto_closed_reviews,
            "recent_model_errors": [
                {
                    "kind": kind,
                    "model": model,
                    "error": error,
                    "count": count,
                    "last_created_at": latest[(kind, model, error)].isoformat(),
                }
                for (kind, model, error), count in error_counts.most_common(50)
            ],
            "instruction": (
                "Review grouped unresolved cases. Canon and IDENTITY_CORE are changed only "
                "through a reviewed versioned patch; provider payloads and user IDs are scrubbed."
            ),
        }

    BehaviorManager.scan = scan  # type: ignore[method-assign]


def _install_actor_policy(actor: Any) -> None:
    # Official lore and mechanics outrank old dialogue. In particular, a previous mistaken
    # “I do not know” reply must not become an established fact merely for continuity.
    original = actor.render

    async def render(self: Any, session: Any, packet: ActorPacket, context: Any, **kwargs: Any) -> Any:
        safe_context = context
        if packet.response_type in {ResponseType.LORE_ANSWER, ResponseType.MECHANICS_ANSWER}:
            safe_context = context.model_copy(update={"recent_messages": []})
        return await original(session, packet, safe_context, **kwargs)

    actor.render = MethodType(render, actor)
    actor.v080_persona_loaded = True
