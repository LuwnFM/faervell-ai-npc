from types import SimpleNamespace

from faervell_npc.discord_bot import FaervellBot
from faervell_npc.services.presence import PresenceService


def test_cross_location_ping_classifier_rejects_test_ping() -> None:
    service = PresenceService()
    assessment = service.assess_cross_location_ping(
        "тест",
        mentioned=True,
        replied_to_bot=False,
    )
    assert assessment.classification == "RANDOM"
    assert assessment.score <= 0.25


def test_cross_location_ping_classifier_schedules_meaningful_summon() -> None:
    service = PresenceService()
    assessment = service.assess_cross_location_ping(
        "Странник, приходи сюда. Мне нужна помощь и есть вопрос.",
        mentioned=True,
        replied_to_bot=False,
    )
    assert assessment.classification == "INTENTIONAL"
    assert assessment.score >= service.settings.traveler_cross_location_min_score


def test_weighted_choice_accepts_legacy_null_probability() -> None:
    service = PresenceService()
    first = SimpleNamespace(appearance_probability=None)
    second = SimpleNamespace(appearance_probability=0.5)

    assert service._weighted_choice([first, second]) in (first, second)


def test_reply_hint_is_spoilered_only_on_last_post() -> None:
    class DummyBot:
        settings = SimpleNamespace(discord_reply_hint_text="Пинганите меня или ответьте на пост.")
        _split_message = staticmethod(FaervellBot._split_message)

    parts = FaervellBot._reply_parts(DummyBot(), "слово " * 800, enabled=True)

    assert len(parts) > 1
    assert all(len(part) <= 1950 for part in parts)
    assert all("||Пинганите" not in part for part in parts[:-1])
    assert parts[-1].endswith("||Пинганите меня или ответьте на пост.||")


def test_reply_hint_can_be_disabled() -> None:
    class DummyBot:
        settings = SimpleNamespace(discord_reply_hint_text="Подсказка")
        _split_message = staticmethod(FaervellBot._split_message)

    parts = FaervellBot._reply_parts(DummyBot(), "Обычный пост", enabled=False)
    assert parts == ["Обычный пост"]


def test_reply_from_previous_visit_is_stale() -> None:
    from datetime import UTC, datetime, timedelta

    arrived_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    old_post = arrived_at - timedelta(minutes=30)

    assert not FaervellBot._reply_belongs_to_current_visit(
        referenced_created_at=old_post,
        arrived_at=arrived_at,
    )


def test_reply_from_current_visit_is_valid() -> None:
    from datetime import UTC, datetime, timedelta

    arrived_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    current_post = arrived_at + timedelta(seconds=5)

    assert FaervellBot._reply_belongs_to_current_visit(
        referenced_created_at=current_post,
        arrived_at=arrived_at,
    )
