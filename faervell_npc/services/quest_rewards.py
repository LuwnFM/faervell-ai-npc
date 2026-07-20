from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import median


@dataclass(frozen=True, slots=True)
class Coin:
    name: str
    value_otn: int


@dataclass(frozen=True, slots=True)
class CurrencySystem:
    name: str
    territories: tuple[str, ...]
    coins: tuple[Coin, ...]


@dataclass(frozen=True, slots=True)
class RewardPreference:
    mode: str
    currency: CurrencySystem | None = None


@dataclass(frozen=True, slots=True)
class RewardQuote:
    quest_type: str
    minimum_otn: int
    maximum_otn: int
    base_otn: int
    mode: str
    reward_text: str
    currency_name: str | None = None
    coin_breakdown: tuple[tuple[str, int], ...] = ()
    item_candidates: tuple[str, ...] = ()


CURRENCY_SYSTEMS: tuple[CurrencySystem, ...] = (
    CurrencySystem(
        "Трайская валюта",
        ("земли трая", "трай"),
        (Coin("трайский ролор", 41000), Coin("трайский сирвол", 2600), Coin("трайский купсон", 200)),
    ),
    CurrencySystem(
        "Эльфийская валюта",
        (
            "эльфийская империя",
            "автократия дергейта",
            "вейский деспотат",
            "свободный город альма",
            "королевство тириуса",
            "вольный город вальтум",
        ),
        (Coin("эльфийский златорун", 26000), Coin("эльфийский люниар", 2000), Coin("эльфийский орумир", 100)),
    ),
    CurrencySystem(
        "Кацианская валюта",
        ("империя кации", "орден святого деметриуса", "церковь мороза", "герцогство акаты"),
        (Coin("кацианский голдинар", 24000), Coin("кацианский серебрум", 1500), Coin("кацианский медролит", 90)),
    ),
    CurrencySystem(
        "Северянская валюта",
        (
            "империя мадези",
            "дал риада",
            "северный пиратский конклав рэма",
            "свободный город харгатрен",
            "империя торад осод",
            "республика ральма",
        ),
        (Coin("северянский ауролин", 19000), Coin("северянский сергор", 1400), Coin("северянский купрол", 30)),
    ),
    CurrencySystem(
        "Кадианская валюта",
        ("кадия",),
        (
            Coin("кадианский матард", 50000),
            Coin("кадианский ауриклин", 13000),
            Coin("кадианский сильвард", 800),
            Coin("кадианский куардон", 15),
        ),
    ),
    CurrencySystem(
        "Стальградская валюта",
        ("стальное королевство", "стальград"),
        (Coin("стальградский матирин", 41000), Coin("стальградский сирклин", 2900), Coin("стальградский рундкуп", 45)),
    ),
    CurrencySystem(
        "Ивелтинская валюта",
        ("республика ивелтин", "ивелтин"),
        (Coin("ивелтинский златарн", 12000), Coin("ивелтинский сертиль", 600), Coin("ивелтинский квадр", 10)),
    ),
    CurrencySystem(
        "Талдрейковская валюта",
        ("герцогство эдеры", "солд-ша", "велдингрейм", "даргейт", "талдрейк", "деспотия долла"),
        (Coin("талдрейковский золонит", 6000), Coin("талдрейковский серролит", 400), Coin("талдрейковский купролит", 6)),
    ),
    CurrencySystem(
        "Звертейловская валюта",
        ("кородовский халифат", "звертейл", "альдейская критархия"),
        (Coin("звертейловский золотин", 4000), Coin("звертейловский аргулит", 350), Coin("звертейловский квадрин", 5)),
    ),
    CurrencySystem(
        "Рунакунская валюта",
        ("империя рунакуны", "рунакуна"),
        (Coin("рунакунский златлит", 3000), Coin("рунакунский центрилсер", 260), Coin("рунакунский медноколь", 4)),
    ),
)


# Диапазоны выражены множителями медианной цены товара в экономической зоне.
# Поэтому они автоматически масштабируются вместе с реальной экономикой, а ОТН
# остаётся единой внутренней единицей.
QUEST_REWARD_MULTIPLIERS: dict[str, tuple[float, float]] = {
    "COLLECT_HERBS": (1.5, 4.0),
    "COLLECT_MINERALS": (2.0, 5.0),
    "COLLECT_WOOD": (1.5, 4.0),
    "COLLECT_COMPONENTS": (2.0, 5.0),
    "GATHER_FOOD": (1.5, 4.0),
    "FISHING": (2.0, 5.0),
    "DELIVER_ITEM": (3.0, 7.0),
    "DELIVER_MESSAGE": (2.0, 5.0),
    "SCOUT_ROUTE": (3.0, 8.0),
    "MAP_AREA": (4.0, 10.0),
    "INVESTIGATE_PLACE": (4.0, 10.0),
    "INVESTIGATE_RUMOR": (4.0, 9.0),
    "ESCORT_TRAVELER": (5.0, 12.0),
    "ESCORT_CARAVAN": (7.0, 16.0),
    "GUARD_CARGO": (6.0, 14.0),
    "CLEAR_ROAD": (5.0, 12.0),
    "DEFEND_LOCATION": (8.0, 18.0),
    "HUNT_BEAST": (8.0, 20.0),
    "DRIVE_OFF_CREATURES": (7.0, 16.0),
    "FIND_MISSING": (7.0, 18.0),
    "RESCUE_PERSON": (10.0, 24.0),
    "CAPTURE_TARGET": (12.0, 28.0),
    "CRAFT_ITEM": (4.0, 10.0),
    "REPAIR_OBJECT": (4.0, 10.0),
    "PREPARE_MEDICINE": (5.0, 12.0),
    "STABILIZE_ANOMALY": (12.0, 28.0),
    "RECOVER_LOST_ITEM": (5.0, 13.0),
    "RECOVER_RELIC": (10.0, 25.0),
    "LORE_EXCHANGE": (5.0, 12.0),
    "TRADE_REQUEST": (3.0, 8.0),
}


_CURRENCY_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "иях",
    "их",
    "ие",
    "ые",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ой",
    "ий",
    "ый",
    "ая",
    "яя",
    "ое",
    "ее",
    "ую",
    "юю",
    "ам",
    "ям",
    "ом",
    "ем",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
)


class QuestRewardService:
    def __init__(self, economy_path: Path) -> None:
        self.path = economy_path

    @staticmethod
    def normalize(value: str) -> str:
        return " ".join(re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).split())

    @classmethod
    def token_stems(cls, value: str) -> set[str]:
        stems: set[str] = set()
        for token in cls.normalize(value).split():
            stem = token
            for suffix in _CURRENCY_SUFFIXES:
                if stem.endswith(suffix) and len(stem) - len(suffix) >= 4:
                    stem = stem[: -len(suffix)]
                    break
            if len(stem) >= 4:
                stems.add(stem)
        return stems

    @classmethod
    def currency_matches(cls, text: str, system: CurrencySystem) -> bool:
        query_stems = cls.token_stems(text)
        if not query_stems:
            return False
        candidates = (system.name, *(coin.name for coin in system.coins))
        for candidate in candidates:
            candidate_stems = cls.token_stems(candidate)
            if candidate_stems and len(query_stems & candidate_stems) >= min(2, len(candidate_stems)):
                return True
        return False

    @staticmethod
    def parse_otn(value: object) -> float | None:
        text = str(value or "").replace("\xa0", " ").replace(" ", "")
        match = re.search(r"-?\d+(?:[.,]\d+)?", text)
        if not match:
            return None
        parsed = float(match.group(0).replace(",", "."))
        return parsed if parsed > 0 else None

    def currencies_for_location(self, location: str) -> tuple[CurrencySystem, ...]:
        normalized = self.normalize(location)
        return tuple(
            system
            for system in CURRENCY_SYSTEMS
            if any(self.normalize(territory) in normalized for territory in system.territories)
        )

    def parse_preference(self, text: str, location: str) -> RewardPreference | None:
        normalized = self.normalize(text)
        if re.search(r"\b(?:предмет|товар|вещ|натур)\w*", normalized):
            return RewardPreference(mode="ITEM")
        if "отн" in normalized:
            return RewardPreference(mode="OTN")
        for system in CURRENCY_SYSTEMS:
            names = (system.name, *(coin.name for coin in system.coins), *system.territories)
            if any(self.normalize(name) in normalized for name in names) or self.currency_matches(
                text, system
            ):
                return RewardPreference(mode="CURRENCY", currency=system)
        local = self.currencies_for_location(location)
        if len(local) == 1 and re.search(r"\b(?:монет|деньг|местн\w+\s+валют)\w*", normalized):
            return RewardPreference(mode="CURRENCY", currency=local[0])
        return None

    @staticmethod
    def looks_like_preference(text: str) -> bool:
        normalized = QuestRewardService.normalize(text)
        if re.search(r"\b(?:валют|монет|деньг|предмет|товар|вещ|натур|отн)\w*", normalized):
            return True
        return any(
            QuestRewardService.normalize(name) in normalized
            for system in CURRENCY_SYSTEMS
            for name in (system.name, *(coin.name for coin in system.coins))
        ) or any(
            QuestRewardService.currency_matches(text, system)
            for system in CURRENCY_SYSTEMS
        )

    def _country_filters(self, location: str) -> tuple[str, ...]:
        systems = self.currencies_for_location(location)
        return tuple(territory for system in systems for territory in system.territories)

    def _price_rows_sync(self, location: str, limit: int = 50000) -> list[tuple[str, str, str, str, str]]:
        if not self.path.is_file():
            return []
        connection = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
        try:
            filters = self._country_filters(location)
            if filters:
                clauses = " OR ".join("country_norm LIKE ?" for _ in filters)
                params: list[object] = [f"%{self.normalize(item)}%" for item in filters]
                params.append(limit)
                query = (
                    "SELECT country,item_name,price_otn,price_currency,quantity "
                    f"FROM economy_items WHERE {clauses} LIMIT ?"
                )
                rows = connection.execute(query, params).fetchall()
                if rows:
                    return [
                        (
                            str(row[0] or ""),
                            str(row[1] or ""),
                            str(row[2] or ""),
                            str(row[3] or ""),
                            str(row[4] or ""),
                        )
                        for row in rows
                    ]
            rows = connection.execute(
                "SELECT country,item_name,price_otn,price_currency,quantity "
                "FROM economy_items LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                (
                    str(row[0] or ""),
                    str(row[1] or ""),
                    str(row[2] or ""),
                    str(row[3] or ""),
                    str(row[4] or ""),
                )
                for row in rows
            ]
        finally:
            connection.close()

    async def reference_price_otn(self, location: str) -> float | None:
        rows = await asyncio.to_thread(self._price_rows_sync, location)
        values = [value for row in rows if (value := self.parse_otn(row[2])) is not None]
        return float(median(values)) if values else None

    @staticmethod
    def _round_otn(value: float) -> int:
        if value <= 100:
            step = 5
        elif value <= 1000:
            step = 10
        elif value <= 10000:
            step = 50
        else:
            step = 100
        return max(step, int(round(value / step) * step))

    @staticmethod
    def convert_otn(amount_otn: int, currency: CurrencySystem) -> tuple[tuple[str, int], ...]:
        remaining = max(0, amount_otn)
        result: list[tuple[str, int]] = []
        for coin in sorted(currency.coins, key=lambda item: item.value_otn, reverse=True):
            count, remaining = divmod(remaining, coin.value_otn)
            if count:
                result.append((coin.name, count))
        return tuple(result)

    async def item_equivalents(self, amount_otn: int, location: str, limit: int = 3) -> tuple[str, ...]:
        rows = await asyncio.to_thread(self._price_rows_sync, location)
        candidates: list[tuple[float, str]] = []
        seen: set[str] = set()
        for country, item_name, price_otn, _price_currency, _quantity in rows:
            parsed = self.parse_otn(price_otn)
            clean_name = item_name.strip()
            if parsed is None or not clean_name or clean_name.casefold() in seen:
                continue
            seen.add(clean_name.casefold())
            count = max(1, int(round(amount_otn / parsed)))
            total_otn = parsed * count
            delta = abs(total_otn - amount_otn) / max(float(amount_otn), 1.0)
            candidates.append(
                (
                    delta,
                    f"{count} × {clean_name} ({country})",
                )
            )
        candidates.sort(key=lambda item: item[0])
        return tuple(text for _delta, text in candidates[:limit])

    async def quote(
        self,
        *,
        quest_type: str,
        location: str,
        preference: RewardPreference,
        seed: str,
    ) -> RewardQuote | None:
        reference = await self.reference_price_otn(location)
        if reference is None:
            return None
        low_multiplier, high_multiplier = QUEST_REWARD_MULTIPLIERS.get(
            quest_type.upper(), (4.0, 10.0)
        )
        minimum = self._round_otn(reference * low_multiplier)
        maximum = max(minimum, self._round_otn(reference * high_multiplier))
        digest = hashlib.sha256(seed.encode()).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        base = self._round_otn(minimum + (maximum - minimum) * fraction)
        base = min(maximum, max(minimum, base))

        if preference.mode == "OTN":
            local = self.currencies_for_location(location)
            preference = (
                RewardPreference(mode="CURRENCY", currency=local[0])
                if len(local) == 1
                else RewardPreference(mode="ITEM")
            )

        if preference.mode == "ITEM":
            items = await self.item_equivalents(base, location)
            item_text = "; ".join(items) if items else "предметы из подтверждённого прайс-листа"
            return RewardQuote(
                quest_type=quest_type,
                minimum_otn=minimum,
                maximum_otn=maximum,
                base_otn=base,
                mode="ITEM",
                reward_text=f"товаром сопоставимой стоимости: {item_text}",
                item_candidates=items,
            )

        if preference.mode == "CURRENCY" and preference.currency is not None:
            smallest = min(coin.value_otn for coin in preference.currency.coins)
            lowest_exact = ((minimum + smallest - 1) // smallest) * smallest
            highest_exact = (maximum // smallest) * smallest
            if lowest_exact <= highest_exact:
                base = min(highest_exact, max(lowest_exact, int(round(base / smallest)) * smallest))
            breakdown = self.convert_otn(base, preference.currency)
            rendered = ", ".join(f"{count} × {name}" for name, count in breakdown)
            return RewardQuote(
                quest_type=quest_type,
                minimum_otn=minimum,
                maximum_otn=maximum,
                base_otn=base,
                mode="CURRENCY",
                reward_text=rendered,
                currency_name=preference.currency.name,
                coin_breakdown=breakdown,
            )

        return RewardQuote(
            quest_type=quest_type,
            minimum_otn=minimum,
            maximum_otn=maximum,
            base_otn=base,
            mode="ITEM",
            reward_text="товаром сопоставимой стоимости",
        )
