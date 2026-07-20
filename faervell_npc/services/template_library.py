from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faervell_npc.config import get_settings


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    id: str
    category: str
    event: str
    quest_type: str | None
    profession_mask: str
    quest_archetype_title: str | None
    tone: tuple[str, ...]
    text: str
    requires_action_result: bool
    required_variables: tuple[str, ...]
    actor_constraints: tuple[str, ...]
    library_status: str = "APPROVED_PERSONA"
    library_reasons: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TemplateRecord:
        return cls(
            id=str(raw.get("id") or ""),
            category=str(raw.get("category") or ""),
            event=str(raw.get("event") or ""),
            quest_type=(str(raw["quest_type"]) if raw.get("quest_type") else None),
            profession_mask=str(raw.get("profession_mask") or "any"),
            quest_archetype_title=(
                str(raw["quest_archetype_title"]) if raw.get("quest_archetype_title") else None
            ),
            tone=tuple(str(value) for value in (raw.get("tone") or [])),
            text=str(raw.get("text") or ""),
            requires_action_result=bool(raw.get("requires_action_result", False)),
            required_variables=tuple(str(value) for value in (raw.get("required_variables") or [])),
            actor_constraints=tuple(str(value) for value in (raw.get("actor_constraints") or [])),
            library_status=str(raw.get("library_status") or "APPROVED_PERSONA"),
            library_reasons=tuple(str(value) for value in (raw.get("library_reasons") or [])),
        )


class TemplateLibrary:
    """Read-only, persona-filtered template and quest-archetype catalog."""

    _MASK_ALIASES = {
        "artisan": "craftsman",
        "merchant": "trader",
        "traveler": "any",
    }
    _QUEST_KEYWORDS: dict[str, tuple[str, ...]] = {
        "COLLECT_HERBS": ("трав", "herb"),
        "COLLECT_MINERALS": ("минерал", "руда", "руд", "камн"),
        "COLLECT_WOOD": ("древес", "дерев", "лес"),
        "COLLECT_COMPONENTS": ("компонент", "образец", "часть"),
        "GATHER_FOOD": ("ед", "припас", "продоволь"),
        "FISHING": ("рыб", "ловл"),
        "SCOUT_ROUTE": ("развед", "маршрут", "дорог", "путь", "перевал"),
        "MAP_AREA": ("карт", "нанести на карту"),
        "INVESTIGATE_PLACE": ("исслед", "осмотр", "проверить место"),
        "INVESTIGATE_RUMOR": ("слух", "проверить сведения"),
        "DELIVER_ITEM": ("достав", "передать предмет", "посыл"),
        "DELIVER_MESSAGE": ("послани", "сообщени", "письм"),
        "ESCORT_TRAVELER": ("сопровод", "провожат", "путник"),
        "ESCORT_CARAVAN": ("караван",),
        "FIND_MISSING": ("пропавш", "разыск", "исчез"),
        "RESCUE_PERSON": ("спас", "вывести", "заложник"),
        "CAPTURE_TARGET": ("захват", "доставить жив", "связать"),
        "HUNT_BEAST": ("охот", "звер"),
        "DRIVE_OFF_CREATURES": ("отогна", "существ", "твар"),
        "DEFEND_LOCATION": ("защит", "оборона", "угроз"),
        "CLEAR_ROAD": ("завал", "освободить путь", "расчист"),
        "CRAFT_ITEM": ("изготов", "создать предмет", "скрафт"),
        "REPAIR_OBJECT": ("почин", "ремонт", "восстановить предмет"),
        "PREPARE_MEDICINE": ("лекарств", "зель", "лечеб"),
        "ACTIVATE_PORTAL": ("портал", "активировать переход"),
        "STABILIZE_ANOMALY": ("аномал", "стабилиз"),
        "RECOVER_LOST_ITEM": ("утерян", "потерян", "вернуть вещь"),
        "RECOVER_RELIC": ("реликв", "артефакт"),
        "LORE_EXCHANGE": ("знани", "сведени", "расскажи больше"),
        "TRADE_REQUEST": ("торг", "обмен", "купить", "продать"),
        "GUARD_CARGO": ("груз", "охранять товар"),
    }
    _OBJECTIVE_TYPES = {
        "COLLECT_HERBS": "COLLECT",
        "COLLECT_MINERALS": "COLLECT",
        "COLLECT_WOOD": "COLLECT",
        "COLLECT_COMPONENTS": "COLLECT",
        "GATHER_FOOD": "COLLECT",
        "FISHING": "COLLECT",
        "CRAFT_ITEM": "CRAFT",
        "PREPARE_MEDICINE": "CRAFT",
        "REPAIR_OBJECT": "REPAIR",
        "DELIVER_ITEM": "DELIVER",
        "DELIVER_MESSAGE": "DELIVER",
        "TRADE_REQUEST": "DELIVER",
        "GUARD_CARGO": "ESCORT",
        "ESCORT_TRAVELER": "ESCORT",
        "ESCORT_CARAVAN": "ESCORT",
        "RESCUE_PERSON": "ESCORT",
        "CAPTURE_TARGET": "ESCORT",
        "HUNT_BEAST": "INVESTIGATE",
        "DRIVE_OFF_CREATURES": "INVESTIGATE",
        "ELIMINATE_MONSTERS": "INVESTIGATE",
        "DEFEND_LOCATION": "INVESTIGATE",
        "INVESTIGATE_PLACE": "INVESTIGATE",
        "INVESTIGATE_RUMOR": "INVESTIGATE",
        "SCOUT_ROUTE": "FIND_LOCATION",
        "MAP_AREA": "FIND_LOCATION",
        "ACTIVATE_PORTAL": "FIND_LOCATION",
        "STABILIZE_ANOMALY": "INVESTIGATE",
        "CLEAR_ROAD": "FIND_LOCATION",
        "RECOVER_LOST_ITEM": "COLLECT",
        "RECOVER_RELIC": "COLLECT",
        "LORE_EXCHANGE": "INVESTIGATE",
    }

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(get_settings().template_library_path)
        self._mtime_ns: int | None = None
        self._templates: list[TemplateRecord] = []
        self._archetypes: dict[str, dict[str, Any]] = {}

    def all(self) -> list[TemplateRecord]:
        self._reload_if_needed()
        return list(self._templates)

    def get(self, template_id: str) -> TemplateRecord | None:
        self._reload_if_needed()
        return next((item for item in self._templates if item.id == template_id), None)

    def quest_archetype(self, quest_type: str | None) -> dict[str, Any]:
        self._reload_if_needed()
        return dict(self._archetypes.get(str(quest_type or "").upper()) or {})

    def is_quest_template_allowed(self, template_id: str, profession_mask_id: str) -> bool:
        record = self.get(template_id)
        if record is None or record.category != "quest_dialogue" or record.event != "offer":
            return False
        if record.library_status == "REJECTED_PERSONA":
            return False
        return self._mask_matches(record.profession_mask, profession_mask_id)

    def is_quest_type_allowed(self, quest_type: str, profession_mask_id: str) -> bool:
        self._reload_if_needed()
        normalized = str(quest_type or "").upper()
        return any(
            item.quest_type == normalized
            and item.event == "offer"
            and item.library_status != "REJECTED_PERSONA"
            and self._mask_matches(item.profession_mask, profession_mask_id)
            for item in self._templates
        )

    def choose_offer(
        self,
        *,
        player_message: str,
        profession_mask_id: str,
        available_variables: set[str] | None = None,
    ) -> TemplateRecord | None:
        self._reload_if_needed()
        available = available_variables or set()
        lowered = player_message.casefold()
        candidates = [
            item
            for item in self._templates
            if item.category == "quest_dialogue"
            and item.event == "offer"
            and item.library_status != "REJECTED_PERSONA"
            and self._mask_matches(item.profession_mask, profession_mask_id)
            and set(item.required_variables).issubset(available)
        ]
        if not candidates:
            candidates = [
                item
                for item in self._templates
                if item.category == "quest_dialogue"
                and item.event == "offer"
                and item.library_status != "REJECTED_PERSONA"
                and self._mask_matches(item.profession_mask, profession_mask_id)
            ]
        if not candidates:
            return None

        def score(item: TemplateRecord) -> tuple[int, int, str]:
            keywords = self._QUEST_KEYWORDS.get(item.quest_type or "", ())
            keyword_score = sum(1 for token in keywords if token.casefold() in lowered)
            profession_score = 2 if item.profession_mask == profession_mask_id else 0
            variable_score = -len(set(item.required_variables) - available)
            return (keyword_score * 10 + profession_score + variable_score, -len(item.id), item.id)

        return max(candidates, key=score)

    def choose_social(self, player_message: str) -> TemplateRecord | None:
        self._reload_if_needed()
        lowered = player_message.casefold()
        keyword_groups = {
            "intro_name_001": ("как тебя зовут", "имя", "звать"),
            "intro_identity_001": ("кто ты", "ты кто", "что ты за"),
            "intro_about_001": ("расскажи о себе", "о себе", "что умеешь"),
            "intro_origin_001": ("откуда ты", "где родился", "пришел"),
            "intro_role_001": ("чем занимаешься", "твоя роль", "кто по профессии"),
            "intro_magic_001": ("какая магия", "способност", "сильный"),
            "intro_memory_001": ("что помнишь", "памят", "забываешь"),
            "intro_trade_001": ("что можешь предложить", "торгуешь", "что продаешь"),
            "intro_age_001": ("сколько лет", "возраст"),
            "intro_god_001": ("бог", "божество", "всемог"),
            "intro_purpose_001": ("зачем ты здесь", "твоя цель", "что ищешь"),
        }
        candidates = [item for item in self._templates if item.category == "social" and item.event == "identity"]
        if not candidates:
            return None
        scored = [
            (sum(1 for token in keyword_groups.get(item.id, ()) if token in lowered), item.id, item)
            for item in candidates
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2] if scored[0][0] else None

    @classmethod
    def objective_type(cls, quest_type: str | None) -> str:
        return cls._OBJECTIVE_TYPES.get(str(quest_type or "").upper(), "INVESTIGATE")

    def _reload_if_needed(self) -> None:
        path = self.root / "templates.stranger.jsonl"
        operational_path = self.root / "operational.stranger.jsonl"
        archetypes_path = self.root / "quest_archetypes.stranger.json"
        if not path.exists():
            self._templates = []
            return
        mtime_ns = max(
            path.stat().st_mtime_ns,
            operational_path.stat().st_mtime_ns if operational_path.exists() else 0,
            archetypes_path.stat().st_mtime_ns if archetypes_path.exists() else 0,
        )
        if self._mtime_ns == mtime_ns:
            return
        loaded: list[TemplateRecord] = []
        for source in (path, operational_path):
            if not source.exists():
                continue
            for line in source.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = TemplateRecord.from_dict(json.loads(line))
                    if record.id and record.library_status != "REJECTED_PERSONA":
                        loaded.append(record)
        self._templates = loaded
        if archetypes_path.exists():
            raw = json.loads(archetypes_path.read_text(encoding="utf-8"))
            self._archetypes = {
                str(item.get("quest_type") or "").upper(): item
                for item in (raw.get("quest_types") or [])
                if item.get("quest_type")
            }
        self._mtime_ns = mtime_ns

    def _mask_matches(self, template_mask: str, requested_mask: str) -> bool:
        template = template_mask.casefold().strip()
        requested = self._MASK_ALIASES.get(requested_mask.casefold().strip(), requested_mask.casefold().strip())
        return template in {"any", requested}


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render_template_text(template: TemplateRecord, variables: dict[str, object]) -> str:
    """Render only supplied values; unresolved placeholders remain explicit."""

    return _PLACEHOLDER_RE.sub(
        lambda match: str(variables[match.group(1)])
        if match.group(1) in variables
        else "[уточняется]",
        template.text,
    )
