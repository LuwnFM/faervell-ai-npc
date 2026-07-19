from __future__ import annotations

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.schemas import (
    ActorPacket,
    QuestDraft,
    QuestObjectiveDraft,
    ResponseType,
    SceneContext,
    ToolRequest,
)
from faervell_npc.services.tools import ToolExecutor


class LocalPlanner:
    QUEST_TERMS = re.compile(r"(?iu)\b(?:квест|задани[ея]|работ[ау]|поручени[ея]|дело)\b")
    QUEST_STOP_WORDS = {
        "квест",
        "задание",
        "задания",
        "работа",
        "работу",
        "поручение",
        "дело",
        "дай",
        "дать",
        "мне",
        "нужно",
        "местное",
        "местный",
        "текущая",
        "локация",
        "локации",
        "местность",
        "дороги",
        "опасности",
        "события",
    }

    def __init__(self, tools: ToolExecutor) -> None:
        self.tools = tools

    async def try_handle(
        self,
        session: AsyncSession,
        *,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket | None:
        lowered = player_message.casefold()

        if "погод" in lowered:
            results = await self.tools.execute_all(
                session,
                [
                    ToolRequest(
                        name="get_world_weather",
                        arguments=json.dumps({}, ensure_ascii=False),
                        purpose="Получить единое внутриигровое состояние погоды",
                    )
                ],
                scene_id=context.scene_id,
                character_id=context.character_id,
                profession_mask_id=context.profession_mask_id,
                location_id=context.location_id,
            )
            weather = results[0].get("result", {}) if results and results[0].get("ok") else {}
            if isinstance(weather, dict) and weather.get("state"):
                return ActorPacket(
                    response_type=ResponseType.DIALOGUE,
                    scene_id=context.scene_id,
                    player_name=context.player_name,
                    profession_mask_id=context.profession_mask_id,
                    location_name=context.location_name,
                    facts_allowed=[
                        f"Сейчас в этой локации: {weather['state']}; {weather.get('detail', '')}."
                    ],
                    action_result={"weather": weather},
                    max_length_words=150,
                )

        if context.active_quests and any(
            phrase in lowered
            for phrase in ("напомни задание", "напомни квест", "что за задание", "какая работа")
        ):
            facts = [
                f"Активный квест «{quest['title']}», статус {quest['status']}."
                for quest in context.active_quests[:3]
            ]
            return ActorPacket(
                response_type=ResponseType.QUEST_PROGRESS,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=facts,
                action_result={"active_quests": context.active_quests[:3]},
                max_length_words=170,
            )

        if self.QUEST_TERMS.search(player_message):
            return await self._grounded_local_quest(session, player_message=player_message, context=context)

        return None

    async def _grounded_local_quest(
        self,
        session: AsyncSession,
        *,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket:
        location = context.location_path or context.location_name or "текущая локация"
        hits = await self.tools.knowledge.search_world(
            session,
            f"{location} местность дороги опасности события {player_message}",
            limit=5,
        )
        relevant_hits = [
            hit
            for hit in hits
            if self._quest_evidence_is_relevant(
                location=location,
                player_message=player_message,
                title=hit.title,
                content=hit.content,
            )
        ]
        if not relevant_hits:
            return await self._pending_review_packet(
                session,
                player_message=player_message,
                context=context,
                location=location,
                reason="Не найдено канонического основания для локального задания",
                candidate_titles=[hit.title for hit in hits[:5]],
            )

        evidence_pool = {
            hit.id: {
                "knowledge_id": hit.id,
                "source_id": hit.source_id,
                "title": hit.title,
                "content": hit.content,
                "corpus": hit.corpus.value,
                "url": hit.url,
            }
            for hit in relevant_hits
        }
        top = relevant_hits[0]
        safe_title = (context.location_name or location).strip("#<> ")
        quest = QuestDraft(
            title=f"Проверить дорогу у {safe_title}",
            template_id="FIND_LOCATION",
            objectives=[QuestObjectiveDraft(id="locate", type="FIND_LOCATION", quantity=1)],
            reward_amount=0,
            repeatable=False,
            gm_approval_required=True,
            evidence=[top.id],
        )
        committed = await self.tools.execute(
            session,
            ToolRequest(
                name="commit_quest",
                arguments=quest.model_dump_json(),
                purpose="Создать локальное задание, подтверждённое источником, и отправить ГМ на одобрение",
            ),
            scene_id=context.scene_id,
            character_id=context.character_id,
            profession_mask_id=context.profession_mask_id,
            location_id=context.location_id,
            evidence_pool=evidence_pool,
        )
        review_id = committed.get("gm_review_request_id") if isinstance(committed, dict) else None
        if not review_id:
            review = await self.tools.execute(
                session,
                ToolRequest(
                    name="create_gm_review",
                    arguments=json.dumps(
                        {
                            "request_type": "QUEST",
                            "reason": "Локальное задание требует служебного подтверждения",
                            "payload": {
                                "player_message": player_message,
                                "location": location,
                                "quest": quest.model_dump(mode="json"),
                                "candidate_source": top.title,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    purpose="Создать служебную заявку для локального задания",
                ),
                scene_id=context.scene_id,
                character_id=context.character_id,
                profession_mask_id=context.profession_mask_id,
                location_id=context.location_id,
                evidence_pool=evidence_pool,
            )
            review_id = review.get("gm_review_request_id") if isinstance(review, dict) else None
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[
                f"У меня есть замысел дела неподалёку от {safe_title}, но сперва нужно уточнить его условия.",
                "Пока не обещаю награду и не отправляю тебя в путь; вернусь к этому предложению, когда всё прояснится.",
            ],
            action_result={
                **(committed if isinstance(committed, dict) else {}),
                "gm_review_request_id": review_id,
            },
            max_length_words=150,
        )

    async def _pending_review_packet(
        self,
        session: AsyncSession,
        *,
        player_message: str,
        context: SceneContext,
        location: str,
        reason: str,
        candidate_titles: list[str],
    ) -> ActorPacket:
        review = await self.tools.execute(
            session,
            ToolRequest(
                name="create_gm_review",
                arguments=json.dumps(
                    {
                        "request_type": "QUEST",
                        "reason": reason,
                        "payload": {
                            "player_message": player_message,
                            "location": location,
                            "candidate_titles": candidate_titles,
                        },
                    },
                    ensure_ascii=False,
                ),
                purpose="Создать служебную заявку для локального задания",
            ),
            scene_id=context.scene_id,
            character_id=context.character_id,
            profession_mask_id=context.profession_mask_id,
            location_id=context.location_id,
            evidence_pool={},
        )
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[
                "Мне нужно сперва уточнить детали этого дела.",
                "Пока не обещаю поручение или награду; вернусь к разговору, когда всё станет ясно.",
            ],
            action_result=review if isinstance(review, dict) else {},
            max_length_words=120,
        )

    @classmethod
    def _quest_evidence_is_relevant(
        cls,
        *,
        location: str,
        player_message: str,
        title: str,
        content: str,
    ) -> bool:
        query_tokens = cls._meaningful_tokens(f"{location} {player_message}")
        if not query_tokens:
            return False
        evidence_tokens = cls._meaningful_tokens(f"{title} {content[:3000]}")
        return bool(query_tokens.intersection(evidence_tokens))

    @classmethod
    def _meaningful_tokens(cls, text: str) -> set[str]:
        tokens = set(re.findall(r"(?iu)[a-zа-яё0-9-]{4,}", text.casefold()))
        return {token for token in tokens if token not in cls.QUEST_STOP_WORDS}
