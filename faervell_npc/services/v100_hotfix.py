from __future__ import annotations

import hashlib
import re
from types import MethodType
from typing import Any, cast

from sqlalchemy import select

from faervell_npc.discord_bot import FaervellBot
from faervell_npc.models import GMReviewRequest, Quest, QuestObjective
from faervell_npc.schemas import (
    ActorPacket,
    QuestDraft,
    QuestObjectiveDraft,
    ResponseType,
    Risk,
    Route,
    RouteDecision,
    SceneContext,
    ToolRequest,
)
from faervell_npc.services.actor import ActorService
from faervell_npc.services.quest_rewards import QuestRewardService, RewardPreference
from faervell_npc.services.template_library import TemplateRecord

HOTFIX_VERSION = "v1.0.0-quest-economy-hotfix.1"

_TEMPLATE_VARIABLES = {
    "quest_title",
    "location_name",
    "location_hint",
    "item_name",
    "quantity",
    "reward_text",
    "next_step",
    "deadline_text",
    "route_name",
    "target_name",
    "exchange_item",
    "price_text",
    "exchange_condition",
    "lore_summary",
    "missing_text",
    "reason_text",
    "progress_current",
    "progress_required",
}

_ITEM_NAMES = {
    "COLLECT_HERBS": "лекарственные травы",
    "COLLECT_MINERALS": "руду и камень",
    "COLLECT_WOOD": "строевую древесину",
    "COLLECT_COMPONENTS": "нужные компоненты",
    "GATHER_FOOD": "дорожные припасы",
    "FISHING": "свежую рыбу",
    "DELIVER_ITEM": "запечатанный груз",
    "DELIVER_MESSAGE": "запечатанное послание",
    "GUARD_CARGO": "опечатанный груз",
    "CRAFT_ITEM": "заказанное изделие",
    "REPAIR_OBJECT": "ремонтные материалы",
    "PREPARE_MEDICINE": "лекарство",
    "RECOVER_LOST_ITEM": "утерянную вещь",
    "RECOVER_RELIC": "реликвию",
}

_DESCRIPTIONS = {
    "COLLECT_HERBS": "Собрать подтверждённые лекарственные травы в указанном районе и вернуться с результатом.",
    "COLLECT_MINERALS": "Добыть подходящую руду или камень в указанном районе и вернуться с результатом.",
    "COLLECT_WOOD": "Заготовить подходящую древесину без вреда для охраняемых мест.",
    "COLLECT_COMPONENTS": "Собрать требуемые компоненты и сохранить их пригодными для использования.",
    "GATHER_FOOD": "Собрать пригодные дорожные припасы и доставить их целыми.",
    "FISHING": "Добыть свежую рыбу в указанном водоёме и сохранить улов.",
    "DELIVER_ITEM": "Доставить запечатанный груз назначенному получателю и получить подтверждение передачи.",
    "DELIVER_MESSAGE": "Передать запечатанное послание адресату и вернуться с подтверждением.",
    "SCOUT_ROUTE": "Проверить проходимость дороги, отметить опасные участки и вернуться с наблюдениями.",
    "MAP_AREA": "Осмотреть район и составить проверяемое описание местности.",
    "INVESTIGATE_PLACE": "Осмотреть указанное место, отделить наблюдаемые факты от слухов и вернуться с результатом.",
    "INVESTIGATE_RUMOR": "Проверить слух на месте и принести только подтверждаемые сведения.",
    "ESCORT_TRAVELER": "Сопроводить путника до указанного места и обеспечить безопасное прибытие.",
    "ESCORT_CARAVAN": "Сопроводить караван по согласованному маршруту и сохранить груз.",
    "GUARD_CARGO": "Сохранить опечатанный груз целым до прибытия в указанное место.",
    "CLEAR_ROAD": "Освободить путь от препятствия либо найти безопасный проверяемый обход.",
    "DEFEND_LOCATION": "Обеспечить защиту указанного места до завершения угрозы.",
    "HUNT_BEAST": "Найти опасного зверя, установить угрозу и устранить её допустимым способом.",
    "DRIVE_OFF_CREATURES": "Отогнать опасных существ от указанного места без лишнего вреда.",
    "FIND_MISSING": "Разыскать пропавшего и установить его состояние.",
    "RESCUE_PERSON": "Найти человека, вывести его из опасности и подтвердить спасение.",
    "CAPTURE_TARGET": "Найти цель и доставить её живой для разбирательства.",
    "CRAFT_ITEM": "Изготовить заказанное изделие по подтверждённым условиям.",
    "REPAIR_OBJECT": "Починить указанный объект и подтвердить его исправность.",
    "PREPARE_MEDICINE": "Приготовить лекарство по точному рецепту без неподтверждённых замен.",
    "STABILIZE_ANOMALY": "Стабилизировать пространственную аномалию и подтвердить результат наблюдением.",
    "RECOVER_LOST_ITEM": "Найти утерянную вещь и вернуть её владельцу.",
    "RECOVER_RELIC": "Найти реликвию, сохранить её целой и доставить для проверки.",
    "LORE_EXCHANGE": "Выполнить оговорённую услугу в обмен на разрешённую часть сведений.",
    "TRADE_REQUEST": "Провести подтверждаемую сделку в заданных пределах стоимости.",
}

_READABLE_OBJECTIVES = {
    "COLLECT": "собрать и сохранить требуемое",
    "CRAFT": "изготовить требуемое по условиям",
    "DELIVER": "доставить и получить подтверждение передачи",
    "INVESTIGATE": "проверить место и сообщить подтверждаемые результаты",
    "FIND_LOCATION": "проверить путь или местность и вернуться с описанием",
    "REPAIR": "починить объект и подтвердить исправность",
    "ESCORT": "сопроводить цель до указанного места",
}


class _TemplateValues(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return ""


def _recent_player_quest(context: SceneContext, current_message: str) -> str | None:
    current = current_message.strip().casefold()
    for item in reversed(context.recent_messages):
        if str(item.get("speaker") or "") not in {"PLAYER", "GM"}:
            continue
        content = str(item.get("content") or "").strip()
        if not content or content.casefold() == current:
            continue
        if re.search(r"(?iu)\b(?:квест|задани[ея]|работ[ау]|поручени[ея]|дело)\b", content):
            return content
    return None


def _forbidden_quest_template(template: TemplateRecord) -> bool:
    quest_type = str(template.quest_type or "").upper()
    searchable = " ".join(
        (
            quest_type,
            str(template.text or ""),
            str(template.quest_archetype_title or ""),
        )
    ).casefold()
    return quest_type == "ACTIVATE_PORTAL" or bool(
        re.search(r"(?iu)\b(?:портал|телепорт)\w*", searchable)
    )


def _sanitize_quest_public_text(text: str) -> str:
    cleaned = re.sub(r"(?iu)\b(?:ОТН|экономическ(?:ая|ой|ую|ий|ого)\s+(?:база|индекс)|индекс\s+экономики)\b", "", text)
    cleaned = re.sub(r"(?iu)\b(?:портал|телепорт)\w*", "дорога", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" \n", "\n", cleaned)
    return cleaned.strip()


def _choose_offer(planner: Any, message: str, context: SceneContext) -> TemplateRecord | None:
    selected = planner.templates.choose_offer(
        player_message=message,
        profession_mask_id=context.profession_mask_id,
        available_variables=_TEMPLATE_VARIABLES,
    )
    if selected is not None and not _forbidden_quest_template(selected):
        return selected

    candidates = [
        item
        for item in planner.templates.all()
        if item.category == "quest_dialogue"
        and item.event == "offer"
        and item.library_status != "REJECTED_PERSONA"
        and set(item.required_variables).issubset(_TEMPLATE_VARIABLES)
        and not _forbidden_quest_template(item)
    ]
    if not candidates:
        return None

    lowered = message.casefold()
    keyword_map = getattr(planner.templates, "_QUEST_KEYWORDS", {})
    scored: list[tuple[int, TemplateRecord]] = []
    for item in candidates:
        score = sum(
            1
            for token in keyword_map.get(item.quest_type or "", ())
            if str(token).casefold() in lowered
        )
        scored.append((score, item))
    best = max(score for score, _item in scored)
    if best > 0:
        return sorted((item for score, item in scored if score == best), key=lambda item: item.id)[0]

    safe_types = {
        "SCOUT_ROUTE",
        "INVESTIGATE_PLACE",
        "COLLECT_HERBS",
        "COLLECT_MINERALS",
        "DELIVER_ITEM",
        "ESCORT_TRAVELER",
        "GUARD_CARGO",
        "CLEAR_ROAD",
        "REPAIR_OBJECT",
    }
    safe = [item for item in candidates if item.quest_type in safe_types] or candidates
    digest = hashlib.sha256(
        f"{context.character_id}:{context.scene_id}:{message}".encode()
    ).digest()
    return sorted(safe, key=lambda item: item.id)[int.from_bytes(digest[:4], "big") % len(safe)]


def _template_values(
    *,
    title: str,
    location: str,
    quest_type: str,
    reward_text: str,
) -> _TemplateValues:
    item_name = _ITEM_NAMES.get(quest_type, "необходимое имущество")
    return _TemplateValues(
        {
            "quest_title": title,
            "location_name": location,
            "location_hint": f"в районе {location}",
            "item_name": item_name,
            "quantity": 1,
            "reward_text": reward_text,
            "next_step": "вернуться с подтверждаемым результатом",
            "deadline_text": "",
            "route_name": location,
            "target_name": "назначенная цель",
            "exchange_item": "равноценный товар",
            "price_text": reward_text,
            "exchange_condition": "выполнить согласованное поручение",
            "lore_summary": "разрешённая часть сведений",
            "missing_text": "не выполнено подтверждаемое условие",
            "reason_text": "условие больше невозможно выполнить",
            "progress_current": 0,
            "progress_required": 1,
        }
    )


def _public_packet_strict(original: Any, packet: ActorPacket) -> dict[str, object]:
    data = original(packet)
    hidden = {
        "gm_review_request_id",
        "requires_gm_approval",
        "gm_reason",
        "review_pending",
        "validation",
        "evidence",
        "template_id",
        "template_event",
        "quest_type",
        "library_status",
        "actor_constraints",
        "required_variables",
        "related_quest_id",
        "raw_payload",
        "exact_template_text",
        "reward_internal",
    }

    def clean(value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key) not in hidden
                and not str(key).startswith("_")
                and "gm_" not in str(key).casefold()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return re.sub(
                r"(?iu)\b(?:PENDING_GM|PENDING_REVIEW|APPROVED_PERSONA|REVIEW_ACTION_RESULT)\b",
                "",
                value,
            ).strip()
        return value

    cleaned = clean(data)
    return cleaned if isinstance(cleaned, dict) else data


async def _build_quest_packet(
    planner: Any,
    session: Any,
    *,
    player_message: str,
    context: SceneContext,
    preference: RewardPreference,
    rewards: QuestRewardService,
) -> ActorPacket:
    location = context.location_path or context.location_name or "текущая локация"
    destination = planner._requested_destination(player_message) or location
    hits = await planner.tools.knowledge.search_world(
        session,
        f"{destination} {location} местность дороги опасности события {player_message}",
        limit=8,
    )
    relevant = [
        hit
        for hit in hits
        if planner._quest_evidence_is_relevant(
            location=destination,
            player_message=player_message,
            title=hit.title,
            content=hit.content,
        )
    ]
    template = _choose_offer(planner, player_message, context)
    if template is None or not template.text:
        text = (
            "Странник ненадолго задумывается.\n\n"
            "— У меня нет подходящего проверенного шаблона для этого дела. "
            "Назови цель точнее: доставка, разведка, сбор, охрана или ремонт."
        )
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[text],
            action_result={"exact_template_text": text},
            max_length_words=120,
        )

    quest_type = str(template.quest_type or "INVESTIGATE_PLACE").upper()
    archetype = planner.templates.quest_archetype(quest_type)
    base_title = str(
        template.quest_archetype_title
        or archetype.get("title")
        or f"Поручение в {destination}"
    )
    title = (
        base_title
        if destination.casefold() in base_title.casefold()
        else f"{base_title}: {destination}"
    )
    quote = await rewards.quote(
        quest_type=quest_type,
        location=destination,
        preference=preference,
        seed=f"{context.character_id}:{context.scene_id}:{player_message}:{template.id}",
    )
    if quote is None:
        text = (
            "Странник закрывает счётную книжку.\n\n"
            "— Сейчас я не могу надёжно сверить размер платы. Выдумывать цену не стану. "
            "Вернёмся к поручению, когда расчёт снова будет доступен."
        )
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[text],
            action_result={"exact_template_text": text},
            max_length_words=120,
        )

    objective_type = planner.templates.objective_type(quest_type)
    objective = QuestObjectiveDraft(
        id=re.sub(r"[^a-z0-9]+", "_", quest_type.casefold()).strip("_") or "objective",
        type=objective_type,  # type: ignore[arg-type]
        quantity=1,
    )
    description = _DESCRIPTIONS.get(
        quest_type,
        f"Выполнить проверяемое поручение в районе {destination} и вернуться с результатом.",
    )
    reward_note = f"Плата после выполнения: {quote.reward_text}."
    quest = QuestDraft(
        title=title,
        template_id=template.id,
        quest_type=quest_type,
        template_event="offer",
        description=description,
        location_name=destination,
        objectives=[objective],
        reward_currency_id="ОТН",
        reward_amount=float(quote.base_otn),
        reward_note=reward_note,
        repeatable=False,
        gm_approval_required=True,
        evidence=[hit.id for hit in relevant[:3]],
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
        for hit in relevant
    }
    committed = await planner.tools.execute(
        session,
        ToolRequest(
            name="commit_quest",
            arguments=quest.model_dump_json(),
            purpose="Создать проверяемый проект задания с подтверждённой наградой",
        ),
        scene_id=context.scene_id,
        character_id=context.character_id,
        profession_mask_id=context.profession_mask_id,
        location_id=context.location_id,
        evidence_pool=evidence_pool,
    )
    result = committed if isinstance(committed, dict) else {}
    if not result.get("committed"):
        text = (
            "Странник убирает метку поручения.\n\n"
            "— Условия пока нельзя подтвердить без надёжной привязки к месту. "
            "Назови конкретный район или цель, и я пересчитаю дело."
        )
        return ActorPacket(
            response_type=ResponseType.DIALOGUE,
            scene_id=context.scene_id,
            player_name=context.player_name,
            profession_mask_id=context.profession_mask_id,
            location_name=context.location_name,
            facts_allowed=[text],
            action_result={
                "exact_template_text": text,
                "gm_review_request_id": result.get("gm_review_request_id"),
            },
            max_length_words=120,
        )

    values = _template_values(
        title=title,
        location=destination,
        quest_type=quest_type,
        reward_text=quote.reward_text,
    )
    rendered = _sanitize_quest_public_text(template.text.format_map(values))
    if not rendered or re.search(r"(?iu)\b(?:портал|телепорт)\w*", rendered):
        rendered = (
            "Странник раскрывает карту и отмечает на ней обычный сухопутный путь.\n\n"
            f"— Есть поручение в районе {destination}: {description} "
            f"Плата после выполнения — {quote.reward_text}."
        )
    rendered += (
        "\n\n— Условия записаны без изменений. "
        "Я вернусь с окончательным словом, когда они будут подтверждены."
    )
    return ActorPacket(
        response_type=ResponseType.QUEST_OFFER,
        scene_id=context.scene_id,
        player_name=context.player_name,
        profession_mask_id=context.profession_mask_id,
        location_name=context.location_name,
        facts_allowed=[description, quote.reward_text, rendered],
        action_result={
            **result,
            "exact_template_text": rendered,
            "reward_internal": {
                "base_otn": quote.base_otn,
                "minimum_otn": quote.minimum_otn,
                "maximum_otn": quote.maximum_otn,
                "mode": quote.mode,
                "currency_name": quote.currency_name,
                "coin_breakdown": list(quote.coin_breakdown),
                "item_candidates": list(quote.item_candidates),
            },
            "quest": quest.model_dump(mode="json"),
        },
        quest_summary=quest,
        max_length_words=260,
    )


def install_v100_hotfix(runtime: Any) -> None:
    if getattr(runtime.orchestrator, "_v100_hotfix_installed", False):
        return
    runtime.orchestrator._v100_hotfix_installed = True

    rewards = QuestRewardService(runtime.economy.path)
    runtime.orchestrator.quest_rewards = rewards

    router = runtime.orchestrator.router
    original_decide = router.decide

    def decide(self: Any, text: str, *, has_active_quest: bool = False) -> RouteDecision:
        if rewards.looks_like_preference(text):
            return RouteDecision(
                route=Route.PLANNER,
                reason="quest_reward_preference",
                risk=Risk.LOW,
                needs_state_change=False,
                confidence=0.99,
            )
        return original_decide(text, has_active_quest=has_active_quest)

    router.decide = MethodType(decide, router)

    planner = runtime.orchestrator.local_planner
    original_try_handle = planner.try_handle

    async def try_handle(
        self: Any,
        session: Any,
        *,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket | None:
        location = context.location_path or context.location_name or ""
        preference = rewards.parse_preference(player_message, location)
        current_is_quest = bool(self.QUEST_TERMS.search(player_message))
        previous_quest = _recent_player_quest(context, player_message)

        if preference is not None and (current_is_quest or previous_quest):
            return await _build_quest_packet(
                self,
                session,
                player_message=player_message if current_is_quest else str(previous_quest),
                context=context,
                preference=preference,
                rewards=rewards,
            )

        if current_is_quest:
            local = rewards.currencies_for_location(location)
            local_text = (
                ", ".join(system.name for system in local)
                if local
                else "любая названная региональная валюта"
            )
            text = (
                "Странник раскрывает небольшую счётную книжку.\n\n"
                f"— Прежде чем закрепить поручение, скажи, как удобнее получить награду: "
                f"{local_text} или товаром сопоставимой стоимости? "
                "Размер платы я назову после сверки условий поручения."
            )
            return ActorPacket(
                response_type=ResponseType.DIALOGUE,
                scene_id=context.scene_id,
                player_name=context.player_name,
                profession_mask_id=context.profession_mask_id,
                location_name=context.location_name,
                facts_allowed=[text],
                action_result={"exact_template_text": text, "reward_preference_requested": True},
                max_length_words=140,
            )

        return await original_try_handle(
            session,
            player_message=player_message,
            context=context,
        )

    planner.try_handle = MethodType(try_handle, planner)

    actor = runtime.orchestrator.actor
    original_render = actor.render

    async def render(
        self: Any,
        session: Any,
        packet: ActorPacket,
        context: SceneContext,
        **kwargs: Any,
    ) -> tuple[str, str | None, str | None]:
        exact = str((packet.action_result or {}).get("exact_template_text") or "").strip()
        if exact:
            return exact, None, "local_template_exact"

        recent_player = next(
            (
                str(item.get("content") or "")
                for item in reversed(context.recent_messages)
                if item.get("speaker") in {"PLAYER", "GM"}
            ),
            "",
        )
        social = runtime.templates.choose_social(recent_player)
        if (
            social is not None
            and packet.response_type == ResponseType.DIALOGUE
            and not packet.facts_allowed
        ):
            values = _TemplateValues({"player_name": context.player_name})
            return social.text.format_map(values).strip(), None, "local_social_template_exact"

        return await original_render(session, packet, context, **kwargs)

    actor.render = MethodType(render, actor)

    actor_service_cls = cast(Any, ActorService)
    if not getattr(actor_service_cls, "_v100_public_packet_installed", False):
        actor_service_cls._v100_public_packet_installed = True
        original_public_packet = actor_service_cls._public_packet
        actor_service_cls._public_packet = staticmethod(
            lambda packet: _public_packet_strict(original_public_packet, packet)
        )

    tools = planner.tools
    original_create_review = tools._create_review_request

    async def create_review_request(
        self: Any,
        session: Any,
        *,
        scene_id: str,
        character_id: str,
        request_type: str,
        reason: str,
        payload: dict[str, Any],
        related_quest_id: str | None = None,
    ) -> GMReviewRequest:
        if related_quest_id:
            existing = (
                await session.execute(
                    select(GMReviewRequest)
                    .where(
                        GMReviewRequest.related_quest_id == related_quest_id,
                        GMReviewRequest.request_type == request_type[:32],
                        GMReviewRequest.status == "PENDING",
                    )
                    .order_by(GMReviewRequest.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        return await original_create_review(
            session,
            scene_id=scene_id,
            character_id=character_id,
            request_type=request_type,
            reason=reason,
            payload=payload,
            related_quest_id=related_quest_id,
        )

    tools._create_review_request = MethodType(create_review_request, tools)

    bot_cls = cast(Any, FaervellBot)
    if not getattr(bot_cls, "_v100_gm_lock_installed", False):
        bot_cls._v100_gm_lock_installed = True
        original_post_review = bot_cls._post_gm_review

        async def post_review_locked(self: Any, review_id: str) -> tuple[bool, str]:
            async with self.runtime.locks.lock(f"gm-review:{review_id}"):
                return await original_post_review(self, review_id)

        bot_cls._post_gm_review = post_review_locked

        def quest_decision_text(
            *,
            approved: bool,
            quest: Quest | None,
            objectives: list[QuestObjective],
            fallback_payload: dict[str, object],
        ) -> str:
            if not approved:
                return "— Это поручение пока не состоится. Я поищу другое дело."

            title = (
                quest.title
                if quest is not None
                else str(fallback_payload.get("title") or "Поручение")
            )
            constraints = dict(quest.constraints or {}) if quest is not None else fallback_payload
            description = str(
                constraints.get("description") or fallback_payload.get("description") or ""
            ).strip()
            location = str(
                constraints.get("location_name") or fallback_payload.get("location_name") or ""
            ).strip()
            reward_note = str(
                constraints.get("reward_note") or fallback_payload.get("reward_note") or ""
            ).strip()

            lines = [f"— Условия ясны. Дело называется «{title}»."]
            if description and not re.search(
                r"(?iu)\b(?:quest_type|template_id|gm_approval|evidence|поручение типа)\b",
                description,
            ):
                lines.append(description)
            if location:
                lines.append(f"Место выполнения: {location}.")
            for objective in objectives[:3]:
                lines.append(
                    "Задача: "
                    + _READABLE_OBJECTIVES.get(
                        objective.objective_type, "выполнить согласованное поручение"
                    )
                    + "."
                )
            if reward_note:
                clean_note = _sanitize_quest_public_text(reward_note)
                if clean_note:
                    lines.append(clean_note)
            else:
                reward = dict(quest.reward or {}) if quest is not None else {}
                amount = reward.get("amount")
                currency = str(reward.get("currency_id") or "").strip()
                if amount and currency:
                    lines.append(f"Плата после выполнения — {float(amount):g} {currency}.")
            lines.append("— Можешь отправляться, когда будешь готова.")
            return "\n".join(lines)

        bot_cls._quest_decision_text = staticmethod(quest_decision_text)
