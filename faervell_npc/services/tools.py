from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import (
    ConversationMessage,
    GMReviewRequest,
    KnowledgeGap,
    Quest,
    QuestObjective,
    RelationshipState,
    SceneConfig,
)
from faervell_npc.schemas import Corpus, QuestDraft, ToolRequest
from faervell_npc.services.disclosure import DisclosureContext, LoreDisclosureEngine
from faervell_npc.services.knowledge import KnowledgeService
from faervell_npc.services.rules import RuleEngine


class ToolExecutor:
    """Validated server-side tool gateway; never accepts SQL or executable code."""

    def __init__(
        self,
        knowledge: KnowledgeService,
        rules: RuleEngine,
        disclosure: LoreDisclosureEngine,
    ) -> None:
        self.knowledge = knowledge
        self.rules = rules
        self.disclosure = disclosure

    async def execute_all(
        self,
        session: AsyncSession,
        requests: list[ToolRequest],
        *,
        scene_id: str,
        character_id: str,
        profession_mask_id: str,
        location_id: str | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        evidence_pool: dict[str, dict[str, Any]] = {}
        for request in requests[:6]:
            try:
                data = await self.execute(
                    session,
                    request,
                    scene_id=scene_id,
                    character_id=character_id,
                    profession_mask_id=profession_mask_id,
                    location_id=location_id,
                    evidence_pool=evidence_pool,
                )
                if request.name in {"search_lore", "search_mechanics"} and isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("knowledge_id"):
                            evidence_pool[str(item["knowledge_id"])] = item
                results.append({"tool": request.name, "ok": True, "result": data})
            except (ValueError, TypeError) as exc:
                results.append({"tool": request.name, "ok": False, "error": str(exc)})
        return results

    async def execute(
        self,
        session: AsyncSession,
        request: ToolRequest,
        *,
        scene_id: str,
        character_id: str,
        profession_mask_id: str,
        location_id: str | None,
        evidence_pool: dict[str, dict[str, Any]],
    ) -> Any:
        try:
            args = json.loads(request.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("tool arguments must be a JSON object string") from exc
        if not isinstance(args, dict):
            raise ValueError("tool arguments must decode to an object")

        if request.name == "search_lore":
            query = self._required_str(args, "query")
            hits = await self.knowledge.search_world(session, query)
            relationship = await session.get(RelationshipState, character_id)
            trust = relationship.trust if relationship else 0.0
            reciprocity = relationship.reciprocity_balance if relationship else 0
            safe_results: list[dict[str, Any]] = []
            for hit in hits:
                decision = self.disclosure.decide(
                    hit,
                    DisclosureContext(
                        player_raised_topic=True,
                        trust=trust,
                        reciprocity_balance=reciprocity,
                    ),
                )
                safe_results.append(
                    {
                        "knowledge_id": hit.id,
                        "source_id": hit.source_id,
                        "title": hit.title,
                        "corpus": hit.corpus.value,
                        "free_summary": decision.free_summary,
                        "content": hit.content if decision.may_disclose else decision.free_summary,
                        "may_disclose": decision.may_disclose,
                        "required_exchange": decision.required_exchange.model_dump(mode="json"),
                        "reason": decision.reason,
                        "url": hit.url,
                    }
                )
            return safe_results

        if request.name == "search_mechanics":
            query = self._required_str(args, "query")
            hits = await self.knowledge.search(session, query, corpus=Corpus.MECHANICS)
            return [
                {
                    "knowledge_id": hit.id,
                    "source_id": hit.source_id,
                    "title": hit.title,
                    "corpus": hit.corpus.value,
                    "content": hit.content,
                    "url": hit.url,
                    "revision": hit.revision,
                }
                for hit in hits
            ]

        if request.name == "get_world_weather":
            return self._deterministic_weather(location_id or "unknown", args.get("game_date"))

        if request.name == "get_market_price":
            return {
                "status": "UNKNOWN_STRUCTURED_PRICE",
                "item_id": args.get("item_id"),
                "zone_id": args.get("economic_zone_id"),
                "instruction": "Use exact MECHANICS evidence or ask GM; never invent a price.",
            }

        if request.name == "check_inventory":
            return {
                "status": "NOT_CONNECTED",
                "character_id": character_id,
                "instruction": "Inventory integration is disabled in MVP; do not claim success.",
            }

        if request.name in {"create_quest_draft", "validate_quest", "commit_quest"}:
            raw = args.get("quest") if request.name == "validate_quest" else args
            quest = QuestDraft.model_validate(raw)
            validation = self.rules.validate_quest(quest, profession_mask_id)
            evidence_errors = self._validate_evidence(quest, evidence_pool)
            validation.errors.extend(evidence_errors)
            validation.valid = validation.valid and not evidence_errors
            response: dict[str, Any] = {
                "draft": quest.model_dump(mode="json"),
                "validation": {
                    "valid": validation.valid,
                    "errors": validation.errors,
                    "requires_gm_approval": validation.requires_gm_approval,
                },
            }
            if request.name == "commit_quest":
                if not validation.valid:
                    response["committed"] = False
                    return response
                status = "PENDING_GM" if validation.requires_gm_approval else "ACTIVE"
                record = Quest(
                    character_id=character_id,
                    scene_id=scene_id,
                    profession_mask_id=profession_mask_id,
                    title=quest.title,
                    template_id=quest.template_id,
                    status=status,
                    reward={
                        "currency_id": quest.reward_currency_id,
                        "amount": quest.reward_amount,
                    },
                    constraints={"repeatable": quest.repeatable},
                    evidence=quest.evidence,
                )
                session.add(record)
                await session.flush()
                for objective in quest.objectives:
                    session.add(
                        QuestObjective(
                            id=f"{record.id}:{objective.id}",
                            quest_id=record.id,
                            objective_type=objective.type,
                            entity_id=objective.entity_id,
                            recipe_id=objective.recipe_id,
                            target_id=objective.target_id,
                            quantity=objective.quantity,
                            depends_on=[f"{record.id}:{dep}" for dep in objective.depends_on],
                        )
                    )
                response.update({"committed": True, "quest_id": record.id, "status": status})
                if status == "PENDING_GM":
                    review = await self._create_review_request(
                        session,
                        scene_id=scene_id,
                        character_id=character_id,
                        request_type="QUEST",
                        reason="quest_requires_gm_approval",
                        payload={"quest": quest.model_dump(mode="json"), "validation": response["validation"]},
                        related_quest_id=record.id,
                    )
                    response["gm_review_request_id"] = review.id
            return response

        if request.name == "create_admin_question":
            question = self._required_str(args, "question")
            gap = KnowledgeGap(
                question=question,
                scene_id=scene_id,
                character_id=character_id,
                profession_mask_id=profession_mask_id,
                evidence=list(args.get("evidence") or []),
            )
            session.add(gap)
            await session.flush()
            review = await self._create_review_request(
                session,
                scene_id=scene_id,
                character_id=character_id,
                request_type="KNOWLEDGE_GAP",
                reason="knowledge_confirmation_required",
                payload={"knowledge_gap_id": gap.id, "question": question, "evidence": gap.evidence},
            )
            return {
                "knowledge_gap_id": gap.id,
                "status": gap.status,
                "gm_review_request_id": review.id,
            }

        if request.name == "create_gm_review":
            reason = self._required_str(args, "reason")
            request_type = str(args.get("request_type") or "GENERAL")[:32]
            review = await self._create_review_request(
                session,
                scene_id=scene_id,
                character_id=character_id,
                request_type=request_type,
                reason=reason,
                payload=dict(args.get("payload") or {}),
            )
            return {"gm_review_request_id": review.id, "status": review.status}

        raise ValueError(f"Unknown tool: {request.name}")

    async def _create_review_request(
        self,
        session: AsyncSession,
        *,
        scene_id: str,
        character_id: str,
        request_type: str,
        reason: str,
        payload: dict[str, Any],
        related_quest_id: str | None = None,
    ) -> GMReviewRequest:
        scene = (
            await session.execute(
                select(SceneConfig).where(SceneConfig.scene_id == scene_id).limit(1)
            )
        ).scalar_one_or_none()
        player = (
            await session.execute(
                select(ConversationMessage.discord_user_id)
                .where(
                    ConversationMessage.scene_id == scene_id,
                    ConversationMessage.character_id == character_id,
                    ConversationMessage.discord_user_id.is_not(None),
                )
                .order_by(ConversationMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        review = GMReviewRequest(
            guild_id=scene.guild_id if scene else "unknown",
            scene_id=scene_id,
            channel_id=scene.channel_id if scene else "unknown",
            player_discord_user_id=player,
            character_id=character_id,
            request_type=request_type[:32],
            reason=reason,
            payload=payload,
            related_quest_id=related_quest_id,
        )
        session.add(review)
        await session.flush()
        return review

    @staticmethod
    def _validate_evidence(
        quest: QuestDraft,
        evidence_pool: dict[str, dict[str, Any]],
    ) -> list[str]:
        errors: list[str] = []
        missing = set(quest.evidence) - set(evidence_pool)
        if missing:
            errors.append("unretrieved_evidence:" + ",".join(sorted(missing)))
        if quest.reward_amount > 0 and not any(
            evidence_pool.get(item, {}).get("corpus") == Corpus.MECHANICS.value
            for item in quest.evidence
        ):
            errors.append("reward_has_no_mechanics_evidence")
        evidence_text = " ".join(json.dumps(evidence_pool.get(item, {}), ensure_ascii=False) for item in quest.evidence).casefold()
        for objective in quest.objectives:
            for entity in (objective.entity_id, objective.recipe_id, objective.target_id):
                if entity and entity.casefold().replace("_", " ") not in evidence_text.replace("_", " "):
                    errors.append(f"unverified_entity:{entity}")
        return errors

    @staticmethod
    def _required_str(args: dict[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _deterministic_weather(location_id: str, game_date: Any) -> dict[str, Any]:
        date_text = str(game_date or datetime.now(UTC).date())
        seed = int(hashlib.sha256(f"{location_id}:{date_text}".encode()).hexdigest()[:8], 16)
        states = [
            ("ясно", "сухо, ветер слабый"),
            ("облачно", "прохладно, ветер умеренный"),
            ("мелкий дождь", "сыро, видимость обычная"),
            ("туман", "видимость снижена"),
            ("ветрено", "порывистый ветер без осадков"),
        ]
        state, detail = states[seed % len(states)]
        return {
            "location_id": location_id,
            "game_date": date_text,
            "state": state,
            "detail": detail,
            "source": "deterministic_mvp_weather",
        }
