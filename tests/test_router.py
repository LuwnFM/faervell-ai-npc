from faervell_npc.schemas import Route
from faervell_npc.services.router import IntentRouter


def test_router_quest_to_planner() -> None:
    decision = IntentRouter().decide("Есть для меня квест по сбору трав?")
    assert decision.route == Route.PLANNER
    assert decision.needs_state_change is True


def test_router_mechanics() -> None:
    decision = IntentRouter().decide("Какая формула цены и сколько нужно ингредиентов?")
    assert decision.route == Route.MECHANICS


def test_router_chat() -> None:
    decision = IntentRouter().decide("Добрый вечер, путник.")
    assert decision.route == Route.CHAT
