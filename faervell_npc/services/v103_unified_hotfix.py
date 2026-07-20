from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Any, cast

from sqlalchemy import select

from faervell_npc.models import GMReviewRequest
from faervell_npc.schemas import (
    ActorPacket,
    ResponseType,
    Risk,
    Route,
    RouteDecision,
    SceneContext,
)
from faervell_npc.services.retrieval_safety import public_contamination_reasons
from faervell_npc.services.trade_offers import (
    TradeDirection,
    TradeIntent,
    TradeOfferDraft,
    TradeSession,
    detect_intent,
    is_payment_answer,
    looks_like_bad_quest_location,
    needs_gm_approval,
    public_offer_text,
)

UNIFIED_HOTFIX_VERSION = "v1.0.3-unified-hotfix.4"
_LOG = logging.getLogger(__name__)
_TRADE_SESSION = TradeSession()
_SCENE_LOCATIONS: dict[tuple[str, str], str] = {}

_CANCEL_RE = re.compile(r"(?iu)^\s*(?:нет|отмена|отмени|не\s+надо|забудь|передумал[аи]?)\b")
_PORTAL_QUEST_TYPES = {"ACTIVATE_PORTAL", "STABILIZE_ANOMALY"}
_QUEST_CATALOG_RE = re.compile(
    r"(?iu)(?:\b(?:какие|какой|список|перечисли|что\s+за)\s+"
    r"(?:типы?\s+)?(?:квест|задани|поручени|работ)\w*|"
    r"\b(?:какие|что)\s+ты\s+(?:можешь\s+)?(?:дать|предложить)\s+"
    r"(?:из\s+)?(?:квест|задани|поручени)\w*)"
)

_ARRIVAL_DETAILS = {
    "default": (
        "Перед собравшимися — высокий худощавый мужчина с бледной, почти "
        "пепельной кожей. По шее и кистям тянутся тонкие линии холодного "
        "бело-голубого света; в центре лба лежит гладкий тёмный камень, "
        "похожий на закрытый третий глаз. На левой руке — браслет из тёмных "
        "пластин, взгляд спокойный и немного утомлённый."
    ),
    "traveler": (
        "На нём выцветший дорожный плащ, у пояса — свёрнутые карты и фляга. "
        "Кожа бледная, почти пепельная; по шее и кистям идут тонкие светящиеся "
        "линии. Во лбу — гладкий тёмный камень, похожий на закрытый глаз, на "
        "левой руке — браслет из тёмных пластин."
    ),
    "merchant": (
        "Поверх практичной одежды лежит торговая перевязь с весами и счётной "
        "книжкой, рядом — аккуратный тюк. Кожа бледная, почти пепельная; по "
        "шее и кистям идут тонкие светящиеся линии. Во лбу — гладкий тёмный "
        "камень, словно закрытый третий глаз, на левой руке — браслет из "
        "тёмных пластин."
    ),
    "herbalist": (
        "Через плечо перекинута сумка с травами; от одежды пахнет сушёным "
        "сбором и смолой. Кожа бледная, почти пепельная; по шее и кистям идут "
        "тонкие светящиеся линии. Во лбу — гладкий тёмный камень, похожий на "
        "закрытый глаз, на левой руке — браслет из тёмных пластин."
    ),
    "artisan": (
        "Руки покрыты мелкими отметинами работы, у пояса свёрнут набор "
        "инструментов. Кожа бледная, почти пепельная; по шее и кистям идут "
        "тонкие светящиеся линии. Во лбу — гладкий тёмный камень, словно "
        "закрытый третий глаз, на левой руке — браслет из тёмных пластин."
    ),
    "guide": (
        "Одежда подобрана под дальний переход, у бедра — футляр с картами и "
        "компас незнакомой работы. Кожа бледная, почти пепельная; по шее и "
        "кистям идут тонкие светящиеся линии. Во лбу — гладкий тёмный камень, "
        "похожий на закрытый глаз, на левой руке — браслет из тёмных пластин."
    ),
}

_ARRIVAL_ACTIVITY = {
    "merchant": "Он раскладывает несколько образцов товара и открывает счётную книжку.",
    "herbalist": "Он устраивается в стороне и проверяет перевязанные пучки трав.",
    "artisan": "Он кладёт рядом свёрток инструментов и осматривает окружающее.",
    "guide": "Он разворачивает карту и сверяет её с дорогами вокруг.",
    "traveler": "Он разглаживает на колене сложенную карту и спокойно осматривается.",
    "default": "Он находит место в стороне, разглаживает карту и спокойно осматривается.",
}


def _dialogue_packet(
    context: SceneContext,
    text: str,
    *,
    action_result: dict[str, object] | None = None,
) -> ActorPacket:
    payload: dict[str, object] = {"exact_template_text": text}
    if action_result:
        payload.update(action_result)
    return ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id=context.scene_id,
        player_name=context.player_name,
        profession_mask_id=context.profession_mask_id,
        location_name=context.location_name,
        facts_allowed=[],
        memories_allowed=[],
        action_result=payload,
        max_length_words=180,
    )


def _location(context: SceneContext) -> str:
    # Для заявок нужен настоящий текущий канал/локация, а не хвост сообщения.
    return str(context.location_name or context.location_path or "текущая локация").strip()


def _quest_catalog_text() -> str:
    return (
        "Странник закрывает счётную книжку и перечисляет без лишних обещаний.\n\n"
        "— Обычно у меня бывают дела на доставку груза или послания; разведку "
        "дороги, местности или слуха; сбор трав, руды, древесины и припасов; "
        "охрану груза и сопровождение; поиск пропавших и спасение; ремонт или "
        "изготовление; защиту места и охоту на опасного зверя. Конкретное дело "
        "зависит от местности и подтверждённых сведений. Назови нужный вид — "
        "после этого согласуем плату."
    )


def _arrival_text(
    *,
    profession_mask_id: str,
    location_name: str | None,
    scene_id: str,
) -> str:
    _ = scene_id
    mask = (profession_mask_id or "default").casefold()
    details = _ARRIVAL_DETAILS.get(mask, _ARRIVAL_DETAILS["default"])
    activity = _ARRIVAL_ACTIVITY.get(mask, _ARRIVAL_ACTIVITY["default"])
    place = f" в {location_name}" if location_name else ""
    return f"*Через некоторое время Странник появляется{place}. {details}\n\n{activity}*"


def _sanitize_direct_quest_text(text: str) -> str:
    """Последний guard для сообщений, которые Discord отправляет без actor.render."""
    if public_contamination_reasons(text):
        return (
            "— В записи смешаны чужие реплики и неподтверждённые сведения. "
            "Такое поручение я не подтвержу."
        )
    safe_lines: list[str] = []
    payment_removed = False
    for line in text.splitlines():
        lowered = line.casefold()
        if any(
            marker in lowered
            for marker in (
                "базовая стоимость награды",
                "расчётный диапазон",
                "экономическая база",
                "экономический индекс",
                "индекс экономики",
            )
        ) or re.search(r"(?iu)\bОТН\b", line):
            payment_removed = payment_removed or any(
                marker in lowered for marker in ("плата", "наград", "стоимость", "диапазон")
            )
            continue
        cleaned = re.sub(
            r"(?iu)портальн(?:ый|ого|ому|ым|ом)\s+узел",
            "старый дорожный узел",
            line,
        )
        cleaned = re.sub(r"(?iu)\bпортал\w*", "дальний переход", cleaned)
        cleaned = re.sub(r"(?iu)\bтелепорт\w*", "быстрый переход", cleaned)
        if cleaned.strip():
            safe_lines.append(cleaned)
    if payment_removed and not any("плата" in item.casefold() for item in safe_lines):
        insert_at = max(1, len(safe_lines) - 1)
        safe_lines.insert(insert_at, "Плата после выполнения — по согласованным условиям.")
    return "\n".join(safe_lines).strip()


def _payment_hint(runtime: Any, context: SceneContext) -> str:
    rewards = getattr(runtime.orchestrator, "quest_rewards", None)
    if rewards is not None:
        try:
            local = rewards.currencies_for_location(_location(context))
            if len(local) == 1 and getattr(local[0], "name", None):
                return f"в {local[0].name} или товаром сопоставимой стоимости"
        except Exception:  # noqa: BLE001 - подсказка не должна ломать диалог
            pass
    return "в названной местной валюте или товаром сопоставимой стоимости"


def _trade_question(runtime: Any, direction: TradeDirection, context: SceneContext) -> str:
    hint = _payment_hint(runtime, context)
    if direction is TradeDirection.NPC_BUYS:
        return (
            "Странник откладывает карту и коротко кивает.\n\n"
            "— Покажи товар и назови количество с качеством. Сразу условие: "
            "выкупаю не дороже восьмидесяти пяти процентов от собственной оценки. "
            f"Расчёт удобнее {hint}?"
        )
    if direction is TradeDirection.BARTER:
        return (
            "Странник проводит пальцами по краю плаща.\n\n"
            "— Обмен возможен. Назови точно, что отдаёшь и что хочешь получить; "
            "после этого я сверю условия."
        )
    return (
        "Странник открывает счётную книжку.\n\n"
        "— Назови вещь точнее: вид, количество и качество. "
        f"Расчёт приму {hint}."
    )


def _trade_payment_prompt(runtime: Any, context: SceneContext) -> str:
    return (
        "Странник записывает уточнение.\n\n"
        "— Товар понял. Теперь назови способ расчёта: "
        f"{_payment_hint(runtime, context)}."
    )


def _review_id(value: object) -> str | None:
    if value is None:
        return None
    direct = getattr(value, "id", None)
    if direct:
        return str(direct)
    if isinstance(value, dict):
        for key in ("gm_review_request_id", "review_id", "id"):
            if value.get(key):
                return str(value[key])
    text = str(value).strip()
    return text or None


async def _create_trade_review(
    planner: Any,
    session: Any,
    draft: TradeOfferDraft,
    context: SceneContext,
) -> str | None:
    creator = getattr(planner.tools, "_create_review_request", None)
    if creator is None:
        _LOG.warning("tradeoffer_review_tool_missing")
        return None
    created = await creator(
        session,
        scene_id=context.scene_id,
        character_id=context.character_id,
        request_type=draft.review_kind,
        reason=draft.review_reason,
        payload=draft.review_payload(),
        related_quest_id=None,
    )
    return _review_id(created)


def _quest_dict(packet: ActorPacket) -> dict[str, Any] | None:
    action = packet.action_result or {}
    quest = action.get("quest") if isinstance(action, dict) else None
    if isinstance(quest, dict):
        return quest
    summary = getattr(packet, "quest_summary", None)
    if summary is not None:
        try:
            return summary.model_dump(mode="json")
        except AttributeError:
            pass
    return None


def _repair_quest_packet(packet: ActorPacket, context: SceneContext) -> ActorPacket:
    quest = _quest_dict(packet)
    if not quest:
        return packet
    bad = str(quest.get("location_name") or "").strip()
    if not looks_like_bad_quest_location(bad):
        return packet
    fallback = _location(context)
    quest["location_name"] = fallback
    title = str(quest.get("title") or "")
    if bad and bad.casefold() in title.casefold():
        quest["title"] = re.sub(re.escape(bad), fallback, title, flags=re.IGNORECASE)
    if isinstance(packet.action_result, dict):
        packet.action_result["quest"] = quest
    summary = getattr(packet, "quest_summary", None)
    if summary is not None and hasattr(summary, "model_copy"):
        packet.quest_summary = summary.model_copy(
            update={
                "location_name": fallback,
                "title": quest.get("title") or getattr(summary, "title", ""),
            }
        )
    _LOG.warning("bad_quest_location_repaired", extra={"bad": bad, "fallback": fallback})
    return packet


def _semantic_payload(request_type: str, payload: dict[str, object]) -> dict[str, object]:
    """Поля, определяющие смысл заявки, без случайной формы выплаты/служебных заметок."""
    if request_type == "QUEST":
        quest_value = payload.get("quest")
        quest = dict(quest_value) if isinstance(quest_value, dict) else {}
        return {
            key: quest.get(key)
            for key in (
                "title",
                "template_id",
                "quest_type",
                "template_event",
                "description",
                "location_name",
                "objectives",
                "reward_currency_id",
                "reward_amount",
                "repeatable",
            )
        }
    if request_type == "TRADEOFFER":
        trade_value = payload.get("trade_offer")
        trade = dict(trade_value) if isinstance(trade_value, dict) else {}
        return {
            key: trade.get(key)
            for key in (
                "direction",
                "player_request",
                "location_name",
                "items",
                "payment",
                "internal_value_otn",
            )
        }
    return payload


def _payload_fingerprint(request_type: str, reason: str, payload: dict[str, object]) -> str:
    rendered = json.dumps(
        {
            "request_type": request_type,
            "reason": reason,
            "payload": _semantic_payload(request_type, payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


async def _find_recent_duplicate_review(
    session: Any,
    *,
    scene_id: str,
    character_id: str,
    request_type: str,
    reason: str,
    payload: dict[str, object],
) -> GMReviewRequest | None:
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    rows = list(
        (
            await session.execute(
                select(GMReviewRequest)
                .where(
                    GMReviewRequest.scene_id == scene_id,
                    GMReviewRequest.character_id == character_id,
                    GMReviewRequest.request_type == request_type,
                    GMReviewRequest.created_at >= cutoff,
                    GMReviewRequest.status.in_(["PENDING", "APPROVED"]),
                )
                .order_by(GMReviewRequest.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    wanted = _payload_fingerprint(request_type, reason, payload)
    for row in rows:
        if request_type == "QUEST" and not row.related_quest_id:
            continue
        existing = _payload_fingerprint(
            str(row.request_type),
            str(row.reason),
            dict(row.payload or {}),
        )
        if existing == wanted:
            return row
    return None


def _duplicate_commit_result(
    review: GMReviewRequest | Any,
    quest: dict[str, object],
) -> dict[str, object]:
    review_payload = dict(getattr(review, "payload", None) or {})
    validation = dict(
        review_payload.get("validation")
        or {
            "valid": True,
            "errors": [],
            "requires_gm_approval": True,
        }
    )
    review_status = str(getattr(review, "status", "PENDING")).upper()
    status = "ACTIVE" if review_status == "APPROVED" else "PENDING_GM"
    review_id = str(review.id)
    quest_id = str(review.related_quest_id)
    return {
        "draft": quest,
        "validation": validation,
        "committed": True,
        "quest_id": quest_id,
        "status": status,
        "gm_review_request_id": review_id,
        "duplicate_of_review_id": review_id,
    }


def _repair_quest_arguments(
    arguments: dict[str, object],
    *,
    scene_id: str,
    character_id: str,
) -> dict[str, object]:
    fixed = dict(arguments)
    bad = str(fixed.get("location_name") or "").strip()
    if not looks_like_bad_quest_location(bad):
        return fixed
    fallback = _SCENE_LOCATIONS.get((scene_id, character_id), "").strip()
    if not fallback:
        return fixed
    fixed["location_name"] = fallback
    title = str(fixed.get("title") or "")
    if bad and bad.casefold() in title.casefold():
        fixed["title"] = re.sub(re.escape(bad), fallback, title, flags=re.IGNORECASE)
    _LOG.warning(
        "bad_quest_location_repaired_before_commit",
        extra={"bad": bad, "fallback": fallback},
    )
    return fixed


def _packet_is_quest(packet: ActorPacket) -> bool:
    return bool(_quest_dict(packet)) or packet.response_type == ResponseType.QUEST_OFFER


def _sanitize_public_text(text: str, packet: ActorPacket) -> str:
    if public_contamination_reasons(text):
        return (
            "*Странник закрывает сомнительную запись и отодвигает её в сторону.*\n\n"
            "— Здесь смешаны чужие реплики и неподтверждённые сведения. "
            "Я не стану выдавать их за правду; нужен другой, чистый источник."
        )

    cleaned = text
    cleaned = re.sub(
        r"(?iu)экономическ(?:ая|ой|ую|ий|ого|ому)\s+(?:база|базы|базу|индекс|индекса)",
        "проверенные расчётные записи",
        cleaned,
    )
    cleaned = re.sub(r"(?iu)\bОТН\b", "расчётной стоимости", cleaned)
    if _packet_is_quest(packet):
        cleaned = re.sub(
            r"(?iu)портальн(?:ый|ого|ому|ым|ом)\s+узел",
            "старый дорожный узел",
            cleaned,
        )
        cleaned = re.sub(r"(?iu)\bпортал\w*", "дальний переход", cleaned)
        cleaned = re.sub(r"(?iu)\bтелепорт\w*", "быстрый переход", cleaned)
    return cleaned


def install_v103_unified_hotfix(runtime: Any) -> None:
    """Устанавливает торгово-квестовый слой поверх v1.0.1 retrieval safety."""
    orchestrator = runtime.orchestrator
    if getattr(orchestrator, "_v103_unified_installed", False):
        return
    orchestrator._v103_unified_installed = True

    router = orchestrator.router
    planner = orchestrator.local_planner
    actor = orchestrator.actor
    tools = planner.tools

    # 1. Торговые фразы и короткие ответы об оплате идут в локальный planner.
    original_decide = router.decide

    def decide(self: Any, text: str, *, has_active_quest: bool = False) -> RouteDecision:
        signal = detect_intent(text)
        if signal.intent is TradeIntent.TRADE or is_payment_answer(text):
            return RouteDecision(
                route=Route.PLANNER,
                reason="v103_trade_or_payment_followup",
                risk=Risk.MEDIUM,
                needs_state_change=True,
                confidence=0.99,
            )
        return original_decide(text, has_active_quest=has_active_quest)

    router.decide = MethodType(decide, router)

    # 2. Не позволяем широкому regex LocalPlanner принимать оплату за локацию.
    original_requested_destination = planner._requested_destination

    def requested_destination(message: str) -> str | None:
        candidate = original_requested_destination(message)
        if candidate and looks_like_bad_quest_location(candidate):
            return None
        return candidate

    planner._requested_destination = requested_destination

    # 3. Не выбираем portal/anomaly-шаблоны для публичных случайных поручений.
    templates = getattr(planner, "templates", None)
    if templates is not None and hasattr(templates, "choose_offer"):
        original_choose_offer = templates.choose_offer

        def choose_offer(self: Any, *args: Any, **kwargs: Any) -> Any:
            selected = original_choose_offer(*args, **kwargs)
            quest_type = str(getattr(selected, "quest_type", "") or "").upper()
            return None if quest_type in _PORTAL_QUEST_TYPES else selected

        templates.choose_offer = MethodType(choose_offer, templates)

    # 4. Торговая память сцены. Квестовый диалог остаётся у v100/v101, чтобы
    # не потерять сохранённый ответ о способе награды.
    original_try_handle = planner.try_handle

    async def try_handle(
        self: Any,
        session: Any,
        *,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket | None:
        _SCENE_LOCATIONS[(context.scene_id, context.character_id)] = _location(context)
        signal = detect_intent(player_message)
        pending = _TRADE_SESSION.get(context.scene_id, context.character_id)

        if _QUEST_CATALOG_RE.search(player_message or ""):
            if pending is not None:
                _TRADE_SESSION.close(context.scene_id, context.character_id)
            return _dialogue_packet(context, _quest_catalog_text())

        if pending is not None:
            if signal.intent is TradeIntent.QUEST:
                _TRADE_SESSION.close(context.scene_id, context.character_id)
            elif _CANCEL_RE.search(player_message or ""):
                _TRADE_SESSION.close(context.scene_id, context.character_id)
                return _dialogue_packet(context, "— Хорошо. Сделку не продолжаем.")
            elif signal.intent is TradeIntent.TRADE:
                # Новая явная торговая просьба заменяет незавершённую.
                _TRADE_SESSION.close(context.scene_id, context.character_id)
            elif is_payment_answer(player_message):
                _TRADE_SESSION.close(context.scene_id, context.character_id)
                payment = " ".join((player_message or "").split())
                draft = TradeOfferDraft(
                    direction=pending.direction,
                    player_request=pending.request,
                    location_name=_location(context),
                    items_text=pending.items_text,
                    internal_value_otn=None,
                    payment_text=payment,
                    requires_gm_approval=needs_gm_approval(None),
                )
                review_id = await _create_trade_review(self, session, draft, context)
                action: dict[str, object] = dict(draft.review_payload())
                if review_id:
                    action["gm_review_request_id"] = review_id
                return _dialogue_packet(context, public_offer_text(draft), action_result=action)
            else:
                _TRADE_SESSION.add_details(
                    context.scene_id,
                    context.character_id,
                    player_message,
                )
                return _dialogue_packet(context, _trade_payment_prompt(runtime, context))

        if signal.intent is TradeIntent.TRADE and signal.direction is not None:
            _TRADE_SESSION.open(
                context.scene_id,
                context.character_id,
                signal.direction,
                " ".join((player_message or "").split()),
            )
            return _dialogue_packet(context, _trade_question(runtime, signal.direction, context))

        packet = await original_try_handle(
            session,
            player_message=player_message,
            context=context,
        )
        return _repair_quest_packet(packet, context) if packet is not None else None

    planner.try_handle = MethodType(try_handle, planner)

    # 5. Последний публичный guard поверх v101: контаминация, служебные
    # экономические термины и portal-лексика в квестах.
    original_render = actor.render

    async def render(
        self: Any,
        session: Any,
        packet: ActorPacket,
        context: SceneContext,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[str, str | None, str | None]:
        text, model, reason = await original_render(session, packet, context, *args, **kwargs)
        return _sanitize_public_text(text, packet), model, reason

    actor.render = MethodType(render, actor)

    # 6. Квесты дедуплицируем до commit_quest: повтор не создаёт второй Quest,
    # objectives или TravelerOpenThread. Ошибочную локацию также исправляем
    # до валидации и записи в БД. TRADEOFFER дедуплицируется при создании review.
    original_execute = tools.execute

    async def execute(
        self: Any,
        session: Any,
        request: Any,
        *,
        scene_id: str,
        character_id: str,
        profession_mask_id: str,
        location_id: str | None,
        evidence_pool: dict[str, dict[str, Any]],
    ) -> Any:
        effective_request = request
        if getattr(request, "name", None) == "commit_quest":
            try:
                raw = json.loads(getattr(request, "arguments", "{}") or "{}")
                if isinstance(raw, dict):
                    quest = _repair_quest_arguments(
                        raw,
                        scene_id=scene_id,
                        character_id=character_id,
                    )
                    if quest != raw:
                        effective_request = request.model_copy(
                            update={
                                "arguments": json.dumps(
                                    quest,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            }
                        )
                    duplicate = await _find_recent_duplicate_review(
                        session,
                        scene_id=scene_id,
                        character_id=character_id,
                        request_type="QUEST",
                        reason="quest_requires_gm_approval",
                        payload={"quest": quest},
                    )
                    if duplicate is not None:
                        _LOG.info(
                            "quest_commit_deduplicated",
                            extra={"review_id": duplicate.id},
                        )
                        return _duplicate_commit_result(duplicate, quest)
            except (json.JSONDecodeError, TypeError):
                # Оригинальный executor вернёт штатную ошибку валидации.
                pass
            except Exception:  # noqa: BLE001 - дедуп не должен блокировать новый квест
                _LOG.exception("quest_commit_dedup_failed")
        return await original_execute(
            session,
            effective_request,
            scene_id=scene_id,
            character_id=character_id,
            profession_mask_id=profession_mask_id,
            location_id=location_id,
            evidence_pool=evidence_pool,
        )

    tools.execute = MethodType(execute, tools)

    original_create_review = tools._create_review_request

    async def create_review_request(
        self: Any,
        session: Any,
        *,
        scene_id: str,
        character_id: str,
        request_type: str,
        reason: str,
        payload: dict[str, object],
        related_quest_id: str | None = None,
    ) -> Any:
        if request_type == "TRADEOFFER":
            try:
                duplicate = await _find_recent_duplicate_review(
                    session,
                    scene_id=scene_id,
                    character_id=character_id,
                    request_type=request_type,
                    reason=reason,
                    payload=payload,
                )
                if duplicate is not None:
                    _LOG.info(
                        "tradeoffer_review_deduplicated",
                        extra={"review_id": duplicate.id},
                    )
                    return duplicate
            except Exception:  # noqa: BLE001 - дедуп не должен блокировать сделку
                _LOG.exception("tradeoffer_review_dedup_failed")
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

    # 7. После решения TRADEOFFER отправляем в исходный канал текст сделки,
    # а не стандартный квестовый ответ «Дело называется…».
    try:
        import discord

        from faervell_npc.db import SessionLocal
        from faervell_npc.discord_bot import FaervellBot

        bot_cls = cast(Any, FaervellBot)
        if not getattr(bot_cls, "_v103_direct_public_guard_installed", False):
            bot_cls._v103_direct_public_guard_installed = True
            bot_cls._arrival_text = staticmethod(_arrival_text)
            original_quest_decision_text = bot_cls._quest_decision_text

            def quest_decision_text(*args: Any, **kwargs: Any) -> str:
                return _sanitize_direct_quest_text(
                    original_quest_decision_text(*args, **kwargs)
                )

            bot_cls._quest_decision_text = staticmethod(quest_decision_text)

        if not getattr(bot_cls, "_v103_trade_decision_installed", False):
            bot_cls._v103_trade_decision_installed = True
            original_decide_gm_review = bot_cls.decide_gm_review

            async def decide_gm_review(
                self: Any,
                interaction: Any,
                review_id: str,
                *,
                approved: bool,
            ) -> None:
                async with SessionLocal() as db_session:
                    review = await db_session.get(GMReviewRequest, review_id)
                    if review is None or review.request_type != "TRADEOFFER":
                        await original_decide_gm_review(
                            self,
                            interaction,
                            review_id,
                            approved=approved,
                        )
                        return
                    if not self._member_is_gm(interaction.user):
                        await interaction.response.send_message(
                            "Решение доступно только ГМ.",
                            ephemeral=True,
                        )
                        return
                    if review.status != "PENDING":
                        await interaction.response.send_message(
                            "Заявка уже обработана.",
                            ephemeral=True,
                        )
                        return
                    review.status = "APPROVED" if approved else "REJECTED"
                    review.decided_by_discord_user_id = str(interaction.user.id)
                    review.decided_at = datetime.now(UTC)
                    source_channel_id = review.channel_id
                    trade = dict((review.payload or {}).get("trade_offer") or {})
                    await db_session.commit()

                current = interaction.message.content if interaction.message else ""
                await interaction.response.edit_message(
                    content=current
                    + f"\n\n**Решение:** {'одобрено' if approved else 'отклонено'} "
                    + f"<@{interaction.user.id}>",
                    view=None,
                )
                if approved:
                    rp_text = (
                        "*Странник ненадолго возвращается к разговору.*\n\n"
                        "— Условия сделки подтверждены. "
                        f"{trade.get('direction_text') or 'Обмен можно завершить'}. "
                        f"Расчёт: {trade.get('payment') or 'по согласованным условиям'}. "
                        "Можем ударить по рукам."
                    )
                else:
                    rp_text = (
                        "*Странник ненадолго возвращается к разговору.*\n\n"
                        "— Эти условия не подтверждены. Сделка в таком виде не состоится. "
                        "Назови другой товар или другой способ расчёта."
                    )
                try:
                    channel = self.get_channel(int(source_channel_id)) or await self.fetch_channel(
                        int(source_channel_id)
                    )
                    if isinstance(channel, (discord.TextChannel, discord.Thread)):
                        await cast(Any, channel).send(rp_text)
                except (ValueError, discord.HTTPException):
                    _LOG.exception(
                        "tradeoffer_decision_delivery_failed",
                        extra={"review_id": review_id},
                    )

            bot_cls.decide_gm_review = decide_gm_review
    except (ImportError, AttributeError):
        _LOG.exception("tradeoffer_discord_patch_not_installed")
        raise

    _LOG.info("v103_unified_hotfix_installed", extra={"version": UNIFIED_HOTFIX_VERSION})
