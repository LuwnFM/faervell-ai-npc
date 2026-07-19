from __future__ import annotations

from faervell_npc.config import Settings
from faervell_npc.schemas import ActorPacket, ResponseType
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.ingest import SourceIngestor
from faervell_npc.services.local_planner import LocalPlanner


def _packet() -> ActorPacket:
    return ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="scene",
        player_name="Игрок",
        max_length_words=300,
    )


def test_actor_priority_prefers_ultra_free() -> None:
    settings = Settings()
    assert settings.effective_actor_models[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_guard_rejects_latin_and_truncation() -> None:
    guard = OutputGuard()
    latin = guard.validate("Странник briefly смотрит на дорогу.", _packet())
    assert "latin_characters_in_rp_body" in latin.violations
    truncated = guard.validate("Странник сжимает зубы,", _packet())
    assert "incomplete_or_truncated_response" in truncated.violations


def test_guard_accepts_finished_russian_scene() -> None:
    result = OutputGuard().validate(
        "Странник отводит клинок телекинезом и отступает на шаг. — Не спеши.",
        _packet(),
    )
    assert result.passed


def test_wikitext_infobox_and_links_are_searchable() -> None:
    sections = SourceIngestor._sections_from_wikitext(
        """
{{Государство
| Правитель = [[Артур II Вир Ивелтин]]
| Расположение = Юго-Западный [[Крорим]]
}}
== История ==
Королевство Ивелтин находится на юго-западе континента.
"""
    )
    joined = "\n".join(body for _, body in sections)
    assert "Правитель: Артур II Вир Ивелтин" in joined
    assert "Расположение: Юго-Западный Крорим" in joined
    assert "Королевство Ивелтин" in joined


def test_destination_quest_is_concrete() -> None:
    planner = LocalPlanner.__new__(LocalPlanner)
    planner.settings = Settings(
        quest_default_reward_amount=5,
        quest_default_reward_currency="местных монет",
    )
    quest = planner._build_quest(
        player_message="Дай мне задание в соседнем регионе — Неживые горы",
        destination="Неживые горы",
        evidence_ids=[],
    )
    assert "Неживые горы" in quest.title
    assert quest.description
    assert quest.objectives
    assert quest.reward_amount == 5
    assert quest.gm_approval_required
