from faervell_npc.schemas import ActorPacket, ResponseType
from faervell_npc.services.guard import OutputGuard


def packet() -> ActorPacket:
    return ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="s1",
        player_name="Эрин",
        facts_allowed=["Награда — 10 монет"],
    )


def test_guard_rejects_unapproved_number() -> None:
    result = OutputGuard().validate("Я дам тебе 999 монет.", packet())
    assert not result.passed


def test_guard_accepts_approved_number() -> None:
    result = OutputGuard().validate("Награда — 10 монет.", packet())
    assert result.passed
