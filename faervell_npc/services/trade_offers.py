from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum

_TRADE_SELL_RE = re.compile(
    r"(?iu)\b(?:прода(?:м|ть|й|ю|жа)|выкупи(?:шь)?|прими(?:те)?|скупа|"
    r"сколько\s+дашь|за\s+сколько\s+(?:возьм|прим)|почём\s+возьм)\w*"
)
_TRADE_BUY_RE = re.compile(
    r"(?iu)\b(?:куплю|купить|продай(?:те)?\s+мне|дай\s+мне|достань|"
    r"нужн(?:ы|о|а)\s+(?:мне\s+)?(?!квест|задани|поручени)|"
    r"есть\s+ли\s+у\s+тебя|что\s+у\s+тебя\s+есть|покажи\s+товар|ассортимент)\w*"
)
_TRADE_BARTER_RE = re.compile(r"(?iu)\b(?:обменя(?:ю|ть|й)|обмен|бартер|меняю)\w*")
_QUEST_RE = re.compile(r"(?iu)\b(?:квест|задани|поручени|контракт|работ[ау])\w*")

# Полное значение location_name должно быть похоже именно на хвост оплаты/задачи.
# Поэтому «Мечевой перевал» не отбрасывается, а «награду три меча» — да.
_BAD_LOCATION_RE = re.compile(
    r"(?iu)^(?:в\s+|на\s+|до\s+)?(?:"
    r"наград(?:а|у|ой)?|оплат(?:а|у|ой)?|плат(?:а|у|ой)?|"
    r"доставк(?:а|у|ой)\s+груз(?:а|у|ом)?|"
    r"три\s+меча|мечи?|злата?рн\w*|сертил\w*|квадр\w*|"
    r"валют\w*|слитк\w*|монет\w*|отн"
    r")(?:\b|\s|$)"
)

# Намеренно не считаем слова «золотой/серебряный» платёжным ответом сами по
# себе: это может быть описание товара («золотой слиток»). Валютный ответ
# должен содержать монеты, валюту, конкретное название или бартер.
_PAYMENT_ANSWER_RE = re.compile(
    r"(?iu)\b(?:монет|валют|злата?рн|сертил|квадр|товаром|предметами|"
    r"бартер|обмен|деньг|оплат|расч[её]т)\w*"
)


class TradeDirection(StrEnum):
    NPC_SELLS = "NPC_SELLS"
    NPC_BUYS = "NPC_BUYS"
    BARTER = "BARTER"


class TradeIntent(StrEnum):
    NONE = "NONE"
    QUEST = "QUEST"
    TRADE = "TRADE"


@dataclass(frozen=True, slots=True)
class TradeSignal:
    intent: TradeIntent
    direction: TradeDirection | None


def detect_intent(message: str) -> TradeSignal:
    """Определяет торговлю, не перехватывая просьбы о квесте."""
    text = message or ""
    if _QUEST_RE.search(text):
        return TradeSignal(TradeIntent.QUEST, None)
    sells = bool(_TRADE_SELL_RE.search(text))
    barter = bool(_TRADE_BARTER_RE.search(text))
    buys = bool(_TRADE_BUY_RE.search(text))
    if barter:
        return TradeSignal(TradeIntent.TRADE, TradeDirection.BARTER)
    if sells:
        return TradeSignal(TradeIntent.TRADE, TradeDirection.NPC_BUYS)
    if buys:
        return TradeSignal(TradeIntent.TRADE, TradeDirection.NPC_SELLS)
    return TradeSignal(TradeIntent.NONE, None)


def is_payment_answer(message: str) -> bool:
    return bool(_PAYMENT_ANSWER_RE.search(message or ""))


def looks_like_bad_quest_location(location_name: str) -> bool:
    value = " ".join((location_name or "").split()).strip(" —-:,.!?")
    if not value:
        return True
    return bool(_BAD_LOCATION_RE.search(value))


BUYBACK_RATE = 0.85
GM_APPROVAL_THRESHOLD_OTN = 50_000.0


@dataclass(slots=True)
class TradeOfferDraft:
    direction: TradeDirection
    player_request: str
    location_name: str
    items_text: str
    internal_value_otn: float | None
    payment_text: str
    requires_gm_approval: bool
    review_reason: str = "trade_requires_gm_approval"
    review_kind: str = "TRADEOFFER"
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def direction_text(self) -> str:
        if self.direction is TradeDirection.NPC_BUYS:
            return "Странник выкупает имущество у игрока"
        if self.direction is TradeDirection.NPC_SELLS:
            return "Странник продаёт или передаёт имущество игроку"
        return "Странник и игрок обмениваются имуществом"

    def review_payload(self) -> dict[str, object]:
        trade_offer: dict[str, object] = {
            "kind": self.review_kind,
            "direction": self.direction.value,
            "direction_text": self.direction_text,
            "player_request": self.player_request,
            "location_name": self.location_name,
            "items": self.items_text,
            "payment": self.payment_text,
            "internal_value_otn": self.internal_value_otn,
            "requires_gm_approval": self.requires_gm_approval,
            **self.extra,
        }
        # Текущий Discord-рендер заявок читает краткие человекочитаемые поля
        # из payload.quest. request_type при этом остаётся TRADEOFFER, а полный
        # объект сделки хранится в trade_offer.
        quest_compat = {
            "title": f"Торговая сделка: {self.items_text[:90]}",
            "description": f"{self.direction_text}. Условия расчёта: {self.payment_text}.",
            "location_name": self.location_name,
            "reward_amount": None,
            "reward_currency_id": None,
        }
        return {"trade_offer": trade_offer, "quest": quest_compat}


def stable_fraction(seed: str) -> float:
    digest = hashlib.sha256(seed.encode()).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def buyback_value(internal_value_otn: float) -> float:
    return round(internal_value_otn * BUYBACK_RATE, 2)


def needs_gm_approval(internal_value_otn: float | None) -> bool:
    return internal_value_otn is None or internal_value_otn >= GM_APPROVAL_THRESHOLD_OTN


def public_offer_text(draft: TradeOfferDraft) -> str:
    """RP-текст без внутренних валютных и инфраструктурных терминов."""
    if draft.direction is TradeDirection.NPC_BUYS:
        body = (
            "Странник осматривает предложенное, не спеша с ответом.\n\n"
            f"— {draft.items_text} — товар ходовой, но полную рыночную цену "
            "не обещаю: выкупаю не дороже восьмидесяти пяти процентов от "
            f"собственной оценки. Расчёт — {draft.payment_text}."
        )
    elif draft.direction is TradeDirection.BARTER:
        body = (
            "Странник раскладывает на плаще то, что готов отдать в обмен.\n\n"
            f"— Условие понял: {draft.items_text}; встречная часть — "
            f"{draft.payment_text}. Разницу, если она возникнет, согласуем товаром."
        )
    else:
        body = (
            "Странник делает короткую пометку в счётной книжке.\n\n"
            f"— Запрос понял: {draft.items_text}. Расчёт — {draft.payment_text}. "
            "Торг возможен в разумных пределах."
        )
    if draft.requires_gm_approval:
        body += (
            "\n\n— Сделка крупная или цена пока не подтверждена. "
            "Прежде чем ударить по рукам, я сверю условия и вернусь с окончательным словом."
        )
    return body


@dataclass(slots=True)
class _PendingTrade:
    direction: TradeDirection
    request: str
    details: list[str]
    created_at: float

    @property
    def items_text(self) -> str:
        parts = [self.request, *self.details]
        return "; ".join(part.strip() for part in parts if part.strip())


class TradeSession:
    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self._ttl = ttl_seconds
        self._pending: dict[tuple[str, str], _PendingTrade] = {}

    @staticmethod
    def _key(scene_id: str, character_id: str) -> tuple[str, str]:
        return (str(scene_id), str(character_id))

    def open(
        self,
        scene_id: str,
        character_id: str,
        direction: TradeDirection,
        request: str,
    ) -> None:
        self._pending[self._key(scene_id, character_id)] = _PendingTrade(
            direction=direction,
            request=request,
            details=[],
            created_at=time.monotonic(),
        )

    def get(self, scene_id: str, character_id: str) -> _PendingTrade | None:
        key = self._key(scene_id, character_id)
        pending = self._pending.get(key)
        if pending is None:
            return None
        if time.monotonic() - pending.created_at > self._ttl:
            del self._pending[key]
            return None
        return pending

    def add_details(self, scene_id: str, character_id: str, text: str) -> _PendingTrade | None:
        pending = self.get(scene_id, character_id)
        value = " ".join((text or "").split())
        if pending is not None and value:
            pending.details.append(value)
            pending.created_at = time.monotonic()
        return pending

    def close(self, scene_id: str, character_id: str) -> None:
        self._pending.pop(self._key(scene_id, character_id), None)
