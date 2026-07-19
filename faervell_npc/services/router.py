from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from faervell_npc.config import get_settings
from faervell_npc.schemas import Risk, Route, RouteDecision


@lru_cache(maxsize=1)
def _rules() -> dict[str, list[str]]:
    path = Path(get_settings().behavior_pack_path) / "routing-rules.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


class IntentRouter:
    def decide(self, text: str, *, has_active_quest: bool = False) -> RouteDecision:
        clean = re.sub(r"<@!?\d+>", "", text).strip()
        rules = _rules()

        planner_terms = rules.get("planner_keywords", [])
        mechanics_terms = rules.get("mechanics_keywords", [])
        lore_terms = rules.get("lore_keywords", [])

        explicit_state_change = _contains_any(clean, planner_terms)
        complex_sequence = len(re.findall(r"\b(?:потом|затем|после|сначала|и ещё)\b", clean.casefold())) >= 2

        if explicit_state_change or complex_sequence:
            return RouteDecision(
                route=Route.PLANNER,
                reason="state_change_or_multi_step_request",
                risk=Risk.MEDIUM if complex_sequence else Risk.LOW,
                needs_state_change=True,
                confidence=0.96,
            )

        if has_active_quest and _contains_any(clean, ["готово", "принёс", "сдал", "выполнил"]):
            return RouteDecision(
                route=Route.PLANNER,
                reason="active_quest_progress",
                risk=Risk.MEDIUM,
                needs_state_change=True,
                confidence=0.98,
            )

        mechanics = _contains_any(clean, mechanics_terms)
        lore = _contains_any(clean, lore_terms)
        if mechanics and lore:
            return RouteDecision(
                route=Route.PLANNER,
                reason="mixed_mechanics_and_lore",
                risk=Risk.LOW,
                needs_state_change=False,
                confidence=0.82,
            )
        if mechanics:
            return RouteDecision(route=Route.MECHANICS, reason="mechanics_keywords", confidence=0.93)
        question_words = [
            "кто", "где", "почему", "что", "какой", "какая", "какое", "когда",
            "с кем", "сколько", "дата", "число", "год", "сезон", "король", "правитель",
        ]
        if lore or (clean.endswith("?") and _contains_any(clean, question_words)) or _contains_any(clean, [
            "кто король", "где находится", "с кем воюет", "какое сейчас число", "какой сейчас год",
        ]):
            return RouteDecision(route=Route.LORE, reason="lore_question", confidence=0.82)
        return RouteDecision(route=Route.CHAT, reason="ordinary_dialogue", confidence=0.90)
