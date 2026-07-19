from __future__ import annotations

import hashlib
from collections.abc import Sequence

_ACTIONS: dict[str, tuple[str, ...]] = {
    "traveler": (
        "проверяет застёжки дорожной сумы",
        "разглаживает на колене сложенную карту",
        "перекладывает кремень и огниво в сухой карман",
        "снимает с сапога налипшую дорожную глину",
        "поправляет перевязь и осматривает край плаща",
        "пересчитывает оставшиеся сухари в холщовом мешочке",
        "прислушивается к шуму вокруг, опираясь на посох",
        "проверяет, не отсырели ли перевязанные письма",
        "сворачивает тонкую верёвку аккуратными кольцами",
        "протирает дорожную флягу краем рукава",
        "разминает затёкшие пальцы после долгого пути",
        "отмечает короткую пометку на полях карты",
    ),
    "merchant": (
        "сверяет записи в небольшой долговой книжке",
        "проверяет вес двух одинаковых на вид монет",
        "перевязывает тесёмкой свёрток с товаром",
        "раскладывает образцы товара по отдельным мешочкам",
        "осматривает сургуч на дорожной накладной",
        "считает свободное место в торговой суме",
    ),
    "guide": (
        "сверяет направление по старой карте",
        "смотрит на небо, оценивая погоду и свет",
        "проверяет зарубки на походном посохе",
        "сравнивает тропу с пометками на полях карты",
        "прислушивается к дальним звукам дороги",
        "перекладывает путевые метки в отдельный карман",
    ),
    "herbalist": (
        "перебирает высушенные травы по запаху",
        "перевязывает треснувший бумажный пакет с листьями",
        "проверяет, не отсырел ли запас кореньев",
        "растирает между пальцами сухой лист",
        "подписывает маленький мешочек с травами",
        "отделяет испорченные стебли от целых",
    ),
    "artisan": (
        "подтягивает ослабший шов на дорожной сумке",
        "правит небольшую костяную иглу",
        "проверяет край ремня на новые трещины",
        "сматывает остаток прочной нити",
        "примеряет новую заклёпку к старому отверстию",
        "очищает инструмент перед тем, как убрать его",
    ),
}

_OPENERS = (
    "Странник на мгновение оставляет своё занятие и переводит взгляд на собеседника.",
    "Странник выслушивает слова до конца, лишь затем поднимая глаза.",
    "Странник отвечает не сразу: сперва оценивает тон сказанного.",
    "Странник чуть поворачивается к собеседнику, сохраняя спокойное выражение лица.",
    "Странник делает короткую паузу и внимательнее всматривается в говорящего.",
    "Странник убирает руки от дорожной мелочи и сосредотачивается на разговоре.",
)


def choose_activity(mask: str, recent_npc_texts: Sequence[str], seed: str) -> str:
    actions = _ACTIONS.get(mask, _ACTIONS["traveler"])
    recent = " ".join(recent_npc_texts[-8:]).casefold()
    unused = [action for action in actions if action.casefold() not in recent]
    pool = unused or list(actions)
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=4).digest()
    return pool[int.from_bytes(digest, "big") % len(pool)]


def choose_opener(recent_npc_texts: Sequence[str], seed: str) -> str:
    recent = " ".join(recent_npc_texts[-6:]).casefold()
    unused = [item for item in _OPENERS if item.casefold() not in recent]
    pool = unused or list(_OPENERS)
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=4).digest()
    return pool[int.from_bytes(digest, "big") % len(pool)]


def arrival_activity(mask: str, seed: str) -> str:
    actions = _ACTIONS.get(mask, _ACTIONS["traveler"])
    digest = hashlib.blake2b(("arrival:" + seed).encode("utf-8"), digest_size=4).digest()
    action = actions[int.from_bytes(digest, "big") % len(actions)]
    return action.replace("проверяет", "проверяя", 1).replace("сверяет", "сверяя", 1).replace(
        "перебирает", "перебирая", 1
    ).replace("подтягивает", "подтягивая", 1).replace("смотрит", "поглядывая", 1)


class StagecraftService:
    """Small deterministic facade used by context and Discord arrival rendering."""

    def choose_activity(self, mask: str, *, scene_id: str, recent_text: str = "") -> str:
        return choose_activity(mask, [recent_text], f"{scene_id}:{len(recent_text)}")

    def choose_opener(self, *, scene_id: str, recent_texts: Sequence[str]) -> str:
        return choose_opener(recent_texts, f"{scene_id}:{len(' '.join(recent_texts))}")

    def arrival_activity(self, mask: str, *, scene_id: str) -> str:
        return arrival_activity(mask, scene_id)
