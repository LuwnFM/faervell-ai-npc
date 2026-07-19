
from faervell_npc.schemas import ActorPacket, ResponseType, SceneContext
from faervell_npc.services.actor import ActorService
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.planner import PlannerService


def _context() -> SceneContext:
    return SceneContext(
        scene_id="scene-1",
        guild_id="guild-1",
        channel_id="channel-1",
        player_name="Путница",
        character_id="character-1",
    )


def test_pending_quest_keeps_internal_review_id_without_ooc_fact() -> None:
    packet = PlannerService._pending_quest_packet(
        _context(),
        {
            "quest_id": "quest-1",
            "status": "PENDING_GM",
            "gm_review_request_id": "review-1",
        },
    )
    assert packet.action_result["gm_review_request_id"] == "review-1"
    joined = " ".join(packet.facts_allowed).casefold()
    assert "гм" not in joined
    assert "gm" not in joined
    assert "заявк" not in joined


def test_actor_public_packet_hides_moderation_fields() -> None:
    packet = ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="scene-1",
        player_name="Путница",
        action_result={
            "quest_id": "quest-1",
            "status": "PENDING_GM",
            "gm_review_request_id": "review-1",
            "requires_gm_approval": True,
            "gm_reason": "test",
        },
        ooc_note="internal",
    )
    public = ActorService._public_packet(packet)
    result = public["action_result"]
    assert isinstance(result, dict)
    assert result["status"] == "PENDING"
    assert "gm_review_request_id" not in result
    assert "requires_gm_approval" not in result
    assert "gm_reason" not in result
    assert public["ooc_note"] is None


def test_output_guard_rejects_ooc_gm_language() -> None:
    packet = ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="scene-1",
        player_name="Путница",
        max_length_words=100,
    )
    result = OutputGuard().validate(
        "Награду должен подтвердить ГМ, заявка уже отправлена.", packet
    )
    assert not result.passed
    assert any(item.startswith("out_of_character_moderation:") for item in result.violations)
