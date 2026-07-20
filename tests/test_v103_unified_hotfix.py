from __future__ import annotations

import asyncio
from types import SimpleNamespace

from faervell_npc.schemas import (
    ActorPacket,
    ResponseType,
    Risk,
    Route,
    RouteDecision,
    SceneContext,
)
from faervell_npc.services import v103_unified_hotfix
from faervell_npc.services.trade_offers import (
    BUYBACK_RATE,
    TradeDirection,
    TradeIntent,
    TradeOfferDraft,
    TradeSession,
    buyback_value,
    detect_intent,
    is_payment_answer,
    looks_like_bad_quest_location,
    needs_gm_approval,
    public_offer_text,
)
from faervell_npc.services.v103_unified_hotfix import (
    _arrival_text,
    _duplicate_commit_result,
    _payload_fingerprint,
    _quest_catalog_text,
    _repair_quest_arguments,
    _sanitize_direct_quest_text,
    _sanitize_public_text,
    install_v103_unified_hotfix,
)


def context() -> SceneContext:
    return SceneContext(
        scene_id="scene",
        character_id="char",
        player_name="Цера",
        profession_mask_id="merchant",
        location_name="Крепкий Овраг",
        location_path="Республика Ивелтин / Крепкий Овраг",
    )


def packet(*, quest: bool = False) -> ActorPacket:
    action = {}
    if quest:
        action = {
            "quest": {
                "title": "Охрана груза: Крепкий Овраг",
                "location_name": "Крепкий Овраг",
            }
        }
    return ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="scene",
        player_name="Цера",
        profession_mask_id="merchant",
        location_name="Крепкий Овраг",
        action_result=action,
    )


def test_quest_words_beat_trade_words() -> None:
    signal = detect_intent("Нет, квест мне дай! А за выполнение в награду три меча.")
    assert signal.intent is TradeIntent.QUEST


def test_trade_directions() -> None:
    assert detect_intent("Продам тебе два слитка").direction is TradeDirection.NPC_BUYS
    assert detect_intent("Дай мне железный меч").direction is TradeDirection.NPC_SELLS
    assert detect_intent("Обменяю шкуры на соль").direction is TradeDirection.BARTER


def test_item_description_is_not_mistaken_for_payment() -> None:
    assert not is_payment_answer("два золотых слитка качества Шедевр")
    assert is_payment_answer("в ивелтинских златарнах")
    assert is_payment_answer("оплата товаром")


def test_bad_locations_from_real_gm_requests() -> None:
    assert looks_like_bad_quest_location("награду три меча")
    assert looks_like_bad_quest_location("доставку груза")
    assert not looks_like_bad_quest_location("Республика Ивелтин")
    assert not looks_like_bad_quest_location("Крепкий Овраг")
    assert not looks_like_bad_quest_location("Мечевой перевал")


def test_buyback_and_gm_threshold() -> None:
    assert BUYBACK_RATE == 0.85
    assert buyback_value(1000) == 850
    assert needs_gm_approval(None)
    assert needs_gm_approval(50_000)
    assert not needs_gm_approval(49_999)


def test_trade_review_payload_is_visible_to_current_gm_renderer() -> None:
    draft = TradeOfferDraft(
        direction=TradeDirection.NPC_BUYS,
        player_request="два золотых слитка",
        location_name="Республика Ивелтин",
        items_text="два золотых слитка качества Шедевр",
        internal_value_otn=None,
        payment_text="ивелтинскими златарнами",
        requires_gm_approval=True,
    )
    payload = draft.review_payload()
    assert payload["trade_offer"]["direction"] == "NPC_BUYS"
    assert payload["quest"]["title"].startswith("Торговая сделка")
    assert payload["quest"]["location_name"] == "Республика Ивелтин"


def test_public_trade_text_has_no_internal_terms() -> None:
    draft = TradeOfferDraft(
        direction=TradeDirection.NPC_BUYS,
        player_request="два слитка",
        location_name="Ивелтин",
        items_text="два слитка",
        internal_value_otn=None,
        payment_text="местной валютой",
        requires_gm_approval=True,
    )
    text = public_offer_text(draft).casefold()
    for forbidden in ("отн", "экономическ", "портал", "телепорт", "индекс"):
        assert forbidden not in text
    assert "восьмидесяти пяти" in text


def test_bad_location_is_repaired_before_commit() -> None:
    v103_unified_hotfix._SCENE_LOCATIONS[("scene", "char")] = "Крепкий Овраг"
    fixed = _repair_quest_arguments(
        {
            "title": "Охрана груза: награду три меча",
            "location_name": "награду три меча",
        },
        scene_id="scene",
        character_id="char",
    )
    assert fixed["location_name"] == "Крепкий Овраг"
    assert fixed["title"] == "Охрана груза: Крепкий Овраг"


def test_duplicate_commit_reuses_existing_quest_and_review() -> None:
    review = SimpleNamespace(
        id="review-existing",
        related_quest_id="quest-existing",
        status="PENDING",
        payload={
            "validation": {
                "valid": True,
                "errors": [],
                "requires_gm_approval": True,
            }
        },
    )
    result = _duplicate_commit_result(review, {"title": "Охрана груза"})
    assert result["quest_id"] == "quest-existing"
    assert result["gm_review_request_id"] == "review-existing"
    assert result["status"] == "PENDING_GM"
    assert result["duplicate_of_review_id"] == "review-existing"


def test_duplicate_quest_ignores_reward_note_realization() -> None:
    base = {
        "quest": {
            "title": "Охрана груза: Крепкий Овраг",
            "template_id": "quest_guard_cargo_offer_01",
            "quest_type": "GUARD_CARGO",
            "template_event": "offer",
            "description": "Сохранить опечатанный груз целым.",
            "location_name": "Крепкий Овраг",
            "objectives": [{"id": "guard_cargo", "type": "ESCORT", "quantity": 1}],
            "reward_currency_id": "ОТН",
            "reward_amount": 284000.0,
            "reward_note": "355 наборов еды",
            "repeatable": False,
        }
    }
    another = {"quest": {**base["quest"], "reward_note": "23 златарна и 13 сертилей"}}
    assert _payload_fingerprint("QUEST", "quest_requires_gm_approval", base) == (
        _payload_fingerprint("QUEST", "quest_requires_gm_approval", another)
    )
    changed = {"quest": {**base["quest"], "reward_amount": 548500.0}}
    assert _payload_fingerprint("QUEST", "quest_requires_gm_approval", base) != (
        _payload_fingerprint("QUEST", "quest_requires_gm_approval", changed)
    )


def test_output_guard_replaces_contamination() -> None:
    unsafe = "Официальные сведения об объекте: Ты сама мне на член прыгнула."
    safe = _sanitize_public_text(unsafe, packet())
    assert "сама мне" not in safe.casefold()
    assert "сомнительную запись" in safe.casefold()


def test_output_guard_hides_portal_only_in_quest() -> None:
    safe = _sanitize_public_text("Маршрут проходит через старый портальный узел.", packet(quest=True))
    assert "портал" not in safe.casefold()
    assert "дорожный узел" in safe.casefold()


def test_trade_session_keeps_item_details_until_payment() -> None:
    session = TradeSession(ttl_seconds=60)
    session.open("scene", "char", TradeDirection.NPC_SELLS, "дай мне мечи железные")
    pending = session.add_details("scene", "char", "два меча качества Хорошее")
    assert pending is not None
    assert "два меча" in pending.items_text
    session.close("scene", "char")
    assert session.get("scene", "char") is None


def test_runtime_wrapper_uses_current_actor_signature_and_preserves_quest_flow() -> None:
    calls: list[tuple[object, object, object]] = []
    planner_calls: list[str] = []

    class Router:
        def decide(self, text: str, *, has_active_quest: bool = False) -> RouteDecision:
            return RouteDecision(route=Route.CHAT, reason="base", risk=Risk.LOW, confidence=1.0)

    class Tools:
        async def execute(
            self,
            session: object,
            request: object,
            **kwargs: object,
        ) -> dict[str, bool]:
            return {"original": True}

        async def _create_review_request(self, session: object, **kwargs: object) -> object:
            return SimpleNamespace(id="review")

    class Planner:
        def __init__(self) -> None:
            self.tools = Tools()
            self.templates = None

        @staticmethod
        def _requested_destination(message: str) -> str | None:
            return "награду три меча" if "награду" in message else None

        async def try_handle(
            self,
            session: object,
            *,
            player_message: str,
            context: SceneContext,
        ) -> ActorPacket | None:
            planner_calls.append(player_message)
            return packet(quest=True) if "квест" in player_message else None

    class Actor:
        async def render(
            self,
            session: object,
            actor_packet: ActorPacket,
            scene_context: SceneContext,
            *args: object,
            **kwargs: object,
        ) -> tuple[str, str | None, str | None]:
            calls.append((session, actor_packet, scene_context))
            return "обычный ответ", "model", "reason"

    class Orchestrator:
        pass

    orchestrator = Orchestrator()
    orchestrator.router = Router()
    orchestrator.local_planner = Planner()
    orchestrator.actor = Actor()
    orchestrator.quest_rewards = None
    runtime = SimpleNamespace(orchestrator=orchestrator)

    install_v103_unified_hotfix(runtime)
    scene = context()
    actor_packet = packet()
    result = asyncio.run(orchestrator.actor.render(object(), actor_packet, scene))
    assert result[0] == "обычный ответ"
    assert len(calls) == 1
    assert orchestrator.local_planner._requested_destination("в награду три меча") is None

    quest_result = asyncio.run(
        orchestrator.local_planner.try_handle(
            object(),
            player_message="дай квест",
            context=scene,
        )
    )
    assert quest_result is not None
    assert planner_calls == ["дай квест"]


def test_quest_catalog_answers_types_instead_of_asking_for_payment() -> None:
    text = _quest_catalog_text().casefold()
    for expected in ("доставку", "разведку", "сбор", "охрану", "поиск", "ремонт"):
        assert expected in text
    for forbidden in ("отн", "экономическ", "портал"):
        assert forbidden not in text


def test_arrival_text_contains_full_persona_and_mask_details() -> None:
    text = _arrival_text(
        profession_mask_id="merchant",
        location_name="домен-бога-бюрократии",
        scene_id="scene",
    ).casefold()
    for expected in (
        "домен-бога-бюрократии",
        "пепельная",
        "светящиеся линии",
        "третий глаз",
        "браслет из тёмных пластин",
        "счётную книжку",
    ):
        assert expected in text


def test_direct_quest_approval_guard_removes_internal_reward_dump() -> None:
    unsafe = (
        "— Условия ясны. Дело называется «Охрана груза: Крепкий Овраг».\n"
        "Место выполнения: Крепкий Овраг.\n"
        "Базовая стоимость награды: 284000 ОТН. Расчётный диапазон этого "
        "поручения: 239400–558600 ОТН.\n"
        "— Можешь отправляться, когда будешь готова."
    )
    safe = _sanitize_direct_quest_text(unsafe)
    lowered = safe.casefold()
    for forbidden in ("отн", "базовая стоимость", "расчётный диапазон"):
        assert forbidden not in lowered
    assert "крепкий овраг" in lowered
    assert "по согласованным условиям" in lowered
