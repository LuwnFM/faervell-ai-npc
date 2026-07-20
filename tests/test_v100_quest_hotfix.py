from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from faervell_npc.services.quest_rewards import (
    CURRENCY_SYSTEMS,
    QuestRewardService,
    RewardPreference,
)


def _economy(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE economy_items (
                country TEXT,
                country_norm TEXT,
                item_name TEXT,
                price_otn TEXT,
                price_currency TEXT,
                quantity TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO economy_items VALUES (?,?,?,?,?,?)",
            [
                ("Республика Ивелтин", "республика ивелтин", "Хлеб", "100", "", "1"),
                ("Республика Ивелтин", "республика ивелтин", "Верёвка", "200", "", "1"),
                ("Республика Ивелтин", "республика ивелтин", "Лекарство", "300", "", "1"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_reward_range_uses_economy_median(tmp_path: Path) -> None:
    database = tmp_path / "economy.sqlite3"
    _economy(database)
    service = QuestRewardService(database)
    currency = next(item for item in CURRENCY_SYSTEMS if item.name == "Ивелтинская валюта")
    quote = await service.quote(
        quest_type="GUARD_CARGO",
        location="Республика Ивелтин",
        preference=RewardPreference(mode="CURRENCY", currency=currency),
        seed="stable",
    )
    assert quote is not None
    assert quote.minimum_otn == 1200
    assert quote.maximum_otn == 2800
    assert quote.minimum_otn <= quote.base_otn <= quote.maximum_otn
    assert quote.coin_breakdown
    assert "местных монет" not in quote.reward_text


@pytest.mark.asyncio
async def test_item_reward_comes_from_price_index(tmp_path: Path) -> None:
    database = tmp_path / "economy.sqlite3"
    _economy(database)
    service = QuestRewardService(database)
    quote = await service.quote(
        quest_type="COLLECT_HERBS",
        location="Республика Ивелтин",
        preference=RewardPreference(mode="ITEM"),
        seed="stable",
    )
    assert quote is not None
    assert quote.item_candidates
    assert all("ОТН" not in item for item in quote.item_candidates)
    assert "ОТН" not in quote.reward_text


def test_currency_converter_uses_official_otn_values() -> None:
    currency = next(item for item in CURRENCY_SYSTEMS if item.name == "Ивелтинская валюта")
    breakdown = QuestRewardService.convert_otn(12610, currency)
    assert breakdown == (
        ("ивелтинский златарн", 1),
        ("ивелтинский сертиль", 1),
        ("ивелтинский квадр", 1),
    )


def test_preference_parser_recognizes_items_and_currency() -> None:
    service = QuestRewardService(Path("missing.sqlite3"))
    assert service.parse_preference("Давай предметами", "Ивелтин").mode == "ITEM"
    preference = service.parse_preference("Удобнее в ивелтинской валюте", "Ивелтин")
    assert preference is not None
    assert preference.currency is not None
    assert preference.currency.name == "Ивелтинская валюта"
    inflected = service.parse_preference("В златарнах ивелтинских", "Ивелтин")
    assert inflected is not None
    assert inflected.currency is not None
    assert inflected.currency.name == "Ивелтинская валюта"
    assert service.looks_like_preference("В златарнах ивелтинских")


@pytest.mark.asyncio
async def test_internal_otn_preference_is_rendered_as_local_currency(tmp_path: Path) -> None:
    database = tmp_path / "economy.sqlite3"
    _economy(database)
    service = QuestRewardService(database)
    quote = await service.quote(
        quest_type="DELIVER_ITEM",
        location="Республика Ивелтин",
        preference=RewardPreference(mode="OTN"),
        seed="stable",
    )
    assert quote is not None
    assert quote.mode == "CURRENCY"
    assert quote.currency_name == "Ивелтинская валюта"
    assert "ОТН" not in quote.reward_text
    assert "экономичес" not in quote.reward_text.casefold()


def test_portal_quest_type_is_not_reward_enabled() -> None:
    from faervell_npc.services.quest_rewards import QUEST_REWARD_MULTIPLIERS

    assert "ACTIVATE_PORTAL" not in QUEST_REWARD_MULTIPLIERS
