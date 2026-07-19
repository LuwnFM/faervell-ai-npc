from pathlib import Path

from faervell_npc.schemas import ActorPacket, ResponseType
from faervell_npc.services.discord_knowledge import (
    _extract_world_date_label,
    _is_structured_world_news,
    _is_world_news_continuation,
)
from faervell_npc.services.v080_grounding import (
    foreign_script_violations,
    is_noise_gap,
    missing_facets,
    normalize_gap_key,
    pending_quest_violations,
    requested_facets,
    safe_evidence,
    ungrounded_lore_claim_violations,
    ungrounded_lore_violations,
)


def packet(**kwargs: object) -> ActorPacket:
    data: dict[str, object] = {
        "response_type": ResponseType.LORE_ANSWER,
        "scene_id": "scene",
        "player_name": "Айша",
        "facts_allowed": [
            "Король Артур Второй Вир Ивелтин — нынешний правитель.",
            "Ивелтин расположен на юго-западе Крорима.",
        ],
    }
    data.update(kwargs)
    return ActorPacket(**data)


def test_compound_lore_question_is_decomposed() -> None:
    query = (
        "Кто король Ивелтина, как туда пройти, какая валюта, "
        "какой сейчас год, дата и время?"
    )
    assert requested_facets(query) == ["ruler", "route", "currency", "year", "date", "time"]
    assert missing_facets(query, packet().facts_allowed) == ["route", "currency", "year", "date", "time"]



def test_location_does_not_fake_an_exact_route() -> None:
    query = "Где находится Ивелтин и как туда пройти?"
    assert requested_facets(query) == ["location", "route"]
    assert missing_facets(query, ["Ивелтин расположен на юго-западе Крорима."]) == ["route"]


def test_partial_answer_is_kept_and_only_missing_parts_are_reported() -> None:
    query = "Кто король Ивелтина, где он находится и какая там валюта?"
    assert missing_facets(query, packet().facts_allowed) == ["currency"]


def test_noise_and_duplicate_gaps_are_normalized() -> None:
    assert is_noise_gap("ты про что щас?")
    assert is_noise_gap("чего что?")
    left = normalize_gap_key("Уточнить: нынешний правитель — Ивелтин.")
    right = normalize_gap_key("Уточнить: правитель нынешний — Ивелтин!")
    assert left == right


def test_foreign_script_and_payload_are_rejected() -> None:
    assert "foreign_script_in_rp_body" in foreign_script_violations("Империалисты — это 당파.")
    assert "structured_payload_in_rp_body" in foreign_script_violations('{"facts_allowed": []}')


def test_ungrounded_brandar_inventions_are_rejected() -> None:
    text = (
        "— На слуху королевство Галтея. После осады «Аргос» ушёл под землю, "
        "а война Кровавая Осень изменила границы."
    )
    violations = ungrounded_lore_violations(text, packet())
    assert violations
    joined = " ".join(violations)
    assert "Галтея" in joined
    assert "Аргос" in joined


def test_grounded_entities_are_allowed() -> None:
    text = "— Король Артур Второй Вир Ивелтин правит Ивелтином на юго-западе Крорима."
    assert not ungrounded_lore_violations(text, packet())
    assert not ungrounded_lore_claim_violations(text, packet())


def test_generic_brandar_inventions_are_rejected_without_new_name() -> None:
    value = packet(facts_allowed=["Брандар — континент."])
    text = "— На Брандаре стран десятка два, а магия запрещена повсеместно."
    assert ungrounded_lore_claim_violations(text, value)


def test_pending_quest_cannot_be_announced_as_active() -> None:
    value = packet(
        action_result={"status": "PENDING_GM"},
        facts_allowed=["Условия ещё нужно уточнить."],
    )
    assert pending_quest_violations("— Условия ясны. Можешь отправляться.", value)


def test_evidence_is_deduplicated_and_service_pages_are_removed() -> None:
    result = safe_evidence(
        [
            {"id": "1", "title": "ИВЕНТОВАЯ ПОЛИТИКА СЕРВЕРА"},
            {"id": "2", "title": "Королевство Ивелтин"},
            {"id": "2", "title": "Королевство Ивелтин"},
        ]
    )
    assert result == [{"id": "2", "title": "Королевство Ивелтин"}]


def test_structured_trusted_news_format_and_world_date() -> None:
    text = (
        "Редакция Отдела ПГМФ представляет новостной отчёт на момент 12.06.1253 года\n"
        "━━━━━━━━》❈《━━━━━━━━\nИвелтин теряет северные провинции\n\n"
        + "Подтверждённые события продолжаются. " * 30
    )
    assert _is_structured_world_news("Лето 1253 года", text)
    assert _extract_world_date_label("Лето 1253 года", text) == "12.06.1253 года"


def test_full_persona_is_installed() -> None:
    root = Path(__file__).resolve().parents[1]
    persona = (root / "behavior-pack/persona.md").read_text(encoding="utf-8")
    assert "Оператор Нулевого Порога" in persona
    assert "Хранилище Потерянного" in persona
    assert "Око Развилки" in persona
    assert "Краткое ядро для модели" in persona
    assert (root / "docs/stranger-persona-source.md").exists()


def test_trusted_author_chat_is_not_news_continuation() -> None:
    assert not _is_world_news_continuation("Всегда рад, сэр")
    assert not _is_world_news_continuation("Скинь хуй.")
    assert _is_world_news_continuation(("Брейвенгейт: обвал нижних уровней\n" + "Подтверждённое описание события. " * 30))
