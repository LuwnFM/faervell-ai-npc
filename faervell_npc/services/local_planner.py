from __future__ import annotations

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.schemas import (
    ActorPacket,
    QuestDraft,
    QuestObjectiveDraft,
    ResponseType,
    SceneContext,
    ToolRequest,
)
from faervell_npc.services.template_library import TemplateLibrary
from faervell_npc.services.tools import ToolExecutor


class LocalPlanner:
    QUEST_TERMS = re.compile(r"(?iu)\b(?:квест|задани[ея]|работ[ау]|поручени[ея]|дело)\b")
    QUEST_DETAILS = re.compile(
        r"(?iu)(?:какое|какая|услови|подроб|расскаж|что\s+(?:делать|сделать)|напомни|суть|награда|оплата)"
    )
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
        "соседнем",
        "соседний",
        "регионе",
        "регион",
    }

    def __init__(self, tools: ToolExecutor, templates: TemplateLibrary | None = None) -> None:
        self.tools = tools
        self.settings = get_settings()
        self.templates = templates or TemplateLibrary()

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

        if context.active_quests and (
            self.QUEST_DETAILS.search(player_message)
            or any(
                phrase in lowered
                for phrase in (
                    "что за задание",
                    "какая работа",
                    "какое дело",
                    "что мне сделать",
                    "чего что",
                )
            )
        ):
            quest = self._best_active_quest(context.active_quests)
            return ActorPacket(
                response_type=ResponseType.QUEST_PROGRESS,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=self._quest_facts(quest),
                action_result={"active_quest": quest},
                max_length_words=210,
            )

        if self.QUEST_TERMS.search(player_message):
            return await self._grounded_local_quest(
                session, player_message=player_message, context=context
            )

        return None

    async def _grounded_local_quest(
        self,
        session: AsyncSession,
        *,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket:
        current_location = context.location_path or context.location_name or "текущая локация"
        destination = self._requested_destination(player_message) or current_location
        hits = await self.tools.knowledge.search_world(
            session,
            f"{destination} {current_location} местность дороги опасности события {player_message}",
            limit=8,
        )
        relevant_hits = [
            hit
            for hit in hits
            if self._quest_evidence_is_relevant(
                location=destination,
                player_message=player_message,
                title=hit.title,
                content=hit.content,
            )
        ]
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
        quest = self._build_quest(
            player_message=player_message,
            destination=destination,
            evidence_ids=[hit.id for hit in relevant_hits[:3]],
            profession_mask_id=context.profession_mask_id,
        )
        committed = await self.tools.execute(
            session,
            ToolRequest(
                name="commit_quest",
                arguments=quest.model_dump_json(),
                purpose="Создать конкретный проект задания и передать его на служебное подтверждение",
            ),
            scene_id=context.scene_id,
            character_id=context.character_id,
            profession_mask_id=context.profession_mask_id,
            location_id=context.location_id,
            evidence_pool=evidence_pool,
        )
        action_result = committed if isinstance(committed, dict) else {}
        if not action_result.get("committed"):
            return await self._review_only_packet(
                session,
                player_message=player_message,
                context=context,
                current_location=current_location,
                quest=quest,
                reason="Проект задания не прошёл автоматическую проверку",
                candidate_titles=[hit.title for hit in hits[:5]],
            )

        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[
                f"Я могу предложить дело «{quest.title}».",
                quest.description,
                "Пока не отправляю тебя в путь: сперва уточню окончательные условия и плату.",
            ],
            action_result={**action_result, "quest": quest.model_dump(mode="json")},
            quest_summary=quest,
            max_length_words=180,
        )

    async def _review_only_packet(
        self,
        session: AsyncSession,
        *,
        player_message: str,
        context: SceneContext,
        current_location: str,
        quest: QuestDraft,
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
                            "location": current_location,
                            "quest": quest.model_dump(mode="json"),
                            "candidate_titles": candidate_titles,
                        },
                    },
                    ensure_ascii=False,
                ),
                purpose="Передать конкретный проект задания на служебное подтверждение",
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
                f"Я наметил дело «{quest.title}».",
                quest.description,
                "Мне нужно уточнить окончательные условия и плату; после этого назову их без недомолвок.",
            ],
            action_result={
                **(review if isinstance(review, dict) else {}),
                "quest": quest.model_dump(mode="json"),
            },
            quest_summary=quest,
            max_length_words=180,
        )

    def _build_quest(
        self,
        *,
        player_message: str,
        destination: str,
        evidence_ids: list[str],
        profession_mask_id: str = "traveler",
    ) -> QuestDraft:
        destination = self._clean_location(destination)
        lowered = player_message.casefold()
        templates = getattr(self, "templates", None) or TemplateLibrary()
        template_record = templates.choose_offer(
            player_message=player_message,
            profession_mask_id=profession_mask_id,
            available_variables={"location_name", "quantity", "quest_title", "next_step"},
        )
        quest_type = (template_record.quest_type if template_record else None) or "INVESTIGATE_PLACE"
        archetype = templates.quest_archetype(quest_type)
        base_title = str(
            (template_record.quest_archetype_title if template_record else None)
            or archetype.get("title")
            or f"Поручение в {destination}"
        )
        title = base_title if destination.casefold() in base_title.casefold() else f"{base_title}: {destination}"
        objective_type = templates.objective_type(quest_type)
        objective = QuestObjectiveDraft(
            id=self._objective_id(quest_type),
            type=objective_type,  # type: ignore[arg-type]
            quantity=1,
        )
        if quest_type == "SCOUT_ROUTE":
            description = (
                f"Добраться до подступов к {destination}, проверить проходимость дороги и "
                "вернуться с кратким описанием опасных участков."
            )
        elif quest_type in {"INVESTIGATE_PLACE", "INVESTIGATE_RUMOR", "MAP_AREA"}:
            description = (
                f"Осмотреть район {destination}, отделить наблюдаемые факты от слухов и "
                "вернуться с проверяемым описанием результата."
            )
        elif quest_type == "COLLECT_HERBS" and "трав" in lowered:
            description = f"Собрать указанные в подтверждённых условиях травы в районе {destination}."
        elif quest_type in {"DELIVER_ITEM", "DELIVER_MESSAGE"} and any(
            token in lowered for token in ("достав", "передач", "посыл", "сообщен")
        ):
            description = f"Передать подтверждённый груз или послание в безопасной точке {destination}."
        else:
            description = (
                f"Выполнить проверяемое поручение типа «{quest_type}» в районе {destination}; "
                "точные предметы и награда фиксируются только после подтверждения условий."
            )
        reward = self.settings.quest_default_reward_amount
        currency = self.settings.quest_default_reward_currency
        return QuestDraft(
            title=title,
            template_id=(template_record.id if template_record else "INVESTIGATE_PLACE"),
            quest_type=quest_type,
            template_event="offer",
            description=description,
            location_name=destination,
            objectives=[objective],
            reward_currency_id=currency,
            reward_amount=reward,
            reward_note="Плата выдаётся после подтверждения выполнения.",
            repeatable=False,
            gm_approval_required=True,
            evidence=evidence_ids,
        )

    @staticmethod
    def _objective_id(quest_type: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", quest_type.casefold()).strip("_") or "objective"

    @staticmethod
    def _best_active_quest(quests: list[dict[str, object]]) -> dict[str, object]:
        for status in ("ACTIVE", "PENDING_GM", "DRAFT"):
            for quest in reversed(quests):
                if quest.get("status") == status:
                    return quest
        return quests[-1]

    @staticmethod
    def _quest_facts(quest: dict[str, object]) -> list[str]:
        facts = [f"Дело называется «{quest.get('title') or 'без названия'}»." ]
        description = str(quest.get("description") or "").strip()
        if description:
            facts.append(description)
        location = str(quest.get("location_name") or "").strip()
        if location:
            facts.append(f"Место выполнения: {location}.")
        reward = quest.get("reward") or {}
        if isinstance(reward, dict) and reward.get("amount"):
            facts.append(
                f"Награда: {float(reward['amount']):g} {reward.get('currency_id') or 'местных монет'}."
            )
        reward_note = str(quest.get("reward_note") or "").strip()
        if reward_note:
            facts.append(reward_note)
        objectives = quest.get("objectives") or []
        if isinstance(objectives, list) and objectives:
            readable = {
                "DELIVER": "доставить пакет и получить подтверждение передачи",
                "FIND_LOCATION": "проверить путь и вернуться с описанием дороги",
                "INVESTIGATE": "осмотреть место и сообщить результаты",
                "ESCORT": "сопроводить путника до указанного места",
            }
            for item in objectives[:3]:
                if isinstance(item, dict):
                    facts.append(f"Задача: {readable.get(str(item.get('type')), 'выполнить поручение')}.")
        status = str(quest.get("status") or "")
        if status == "PENDING_GM":
            facts.append("Окончательные условия ещё уточняются; в путь пока не отправляю.")
        elif status == "ACTIVE":
            facts.append("Условия подтверждены, задание можно выполнять.")
        return facts

    @staticmethod
    def _requested_destination(message: str) -> str | None:
        patterns = (
            r"(?iu)(?:регион\w*|локаци\w*|местност\w*)\s*[-—:]\s*([^,.!?\n]{3,70})",
            r"(?iu)\b(?:в|до|на)\s+(?:соседн\w+\s+(?:регион\w*|локаци\w*)\s*)?([^,.!?\n]{3,70})",
        )
        for pattern in patterns:
            match = re.search(pattern, message)
            if not match:
                continue
            candidate = LocalPlanner._clean_location(match.group(1))
            if candidate and candidate.casefold() not in {"текущей локации", "этом месте"}:
                return candidate
        return None

    @staticmethod
    def _clean_location(value: str) -> str:
        value = re.sub(r"[<>#]", "", value)
        value = re.sub(r"(?iu)\b(?:ведь|пожалуйста|мне|любое|любой|задание)\b.*$", "", value)
        value = re.sub(r"\s+", " ", value).strip(" —-:,.!?\n\t")
        return value or "текущей локации"

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
        # A generic economy page is not enough to ground a location quest. At least one
        # destination/name token must be present in the source title or body.
        location_tokens = cls._meaningful_tokens(location)
        return bool(query_tokens.intersection(evidence_tokens)) and (
            not location_tokens or bool(location_tokens.intersection(evidence_tokens))
        )

    @classmethod
    def _meaningful_tokens(cls, text: str) -> set[str]:
        tokens = set(re.findall(r"(?iu)[a-zа-яё0-9-]{4,}", text.casefold()))
        return {token for token in tokens if token not in cls.QUEST_STOP_WORDS}
