from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.schemas import ActorPacket, ResponseType, SceneContext, ToolRequest
from faervell_npc.services.tools import ToolExecutor


class LocalPlanner:
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

        return None
