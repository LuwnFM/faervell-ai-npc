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
        if not hits:
            review = await self.tools.execute(
                session,
                ToolRequest(
                    name="create_gm_review",
                    arguments=json.dumps(
                        {
                            "request_type": "QUEST",
                            "reason": "Не найдено канонического основания для локального задания",
                            "payload": {
                                "player_message": player_message,
                                "location": location,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    purpose="Передать запрос на локальное задание ГМ",
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
                facts_allowed=["Запрос на местное задание передан распорядителям этой локации."],
                action_result=review,
                max_length_words=120,
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
            for hit in hits
        }
        top = hits[0]
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
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[
                f"У меня есть местное дело у {safe_title}, но награду и условия должен подтвердить ГМ.",
                f"Основание: сведения из «{top.title}».",
            ],
            action_result={
                **(committed if isinstance(committed, dict) else {}),
                "gm_review_request_id": review_id,
            },
            max_length_words=150,
        )
