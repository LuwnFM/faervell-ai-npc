from pathlib import Path

from faervell_npc.discord_bot import FaervellBot
from faervell_npc.schemas import ActorPacket, ResponseType
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.knowledge import KnowledgeService


def test_title_terms_keep_entity_and_drop_generic_fields() -> None:
    query = "Где находится Королевство Ивелтин и кто сейчас им правит?"
    assert KnowledgeService._query_terms(query) == ["ивелтин"]
    assert KnowledgeService._title_qualifiers(query) == ["королевств"]


def test_inflected_entity_name_is_normalized_and_king_implies_kingdom() -> None:
    query = "кто король Ивелтина"
    assert KnowledgeService._query_terms(query) == ["ивелтин"]
    assert KnowledgeService._title_qualifiers(query) == ["королевств"]


def test_republic_and_kingdom_are_disambiguated() -> None:
    assert KnowledgeService._title_qualifiers("Республика Ивелтин") == ["республик"]
    assert KnowledgeService._title_qualifiers("Королевство Ивелтин") == ["королевств"]


def test_public_rp_does_not_append_source_titles() -> None:
    text = "— Ивелтин расположен на юго-западе."
    rendered = FaervellBot._with_sources(
        text,
        [{"title": "Королевство Ивелтин", "url": "https://example.invalid"}],
    )
    assert rendered == text
    assert "Источники" not in rendered


def test_output_guard_rejects_source_footer() -> None:
    packet = ActorPacket(
        response_type=ResponseType.LORE_ANSWER,
        scene_id="scene",
        player_name="Игрок",
        profession_mask_id="traveler",
        location_name="Локация",
        max_length_words=100,
    )
    result = OutputGuard().validate(
        "— Ивелтин расположен на юго-западе.\n\nИсточники: «Королевство Ивелтин».",
        packet,
    )
    assert not result.passed
    assert "out_of_character_moderation:source_footer" in result.violations


def test_docker_image_contains_architecture_source() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY docs ./docs" in dockerfile
    architecture = root / "docs" / "architecture-source.md"
    assert architecture.exists()
    assert "Версия системы:** `0.7.4`" in architecture.read_text(encoding="utf-8")
