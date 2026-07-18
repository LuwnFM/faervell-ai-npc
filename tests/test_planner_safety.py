from faervell_npc.schemas import (
    PlannerPlan,
    ResponseType,
    Risk,
    SceneContext,
    ToolRequest,
)
from faervell_npc.services.actor import ActorService
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.planner import PlannerService


def context() -> SceneContext:
    return SceneContext(
        scene_id="s1",
        character_id="pc1",
        player_name="Эрин",
        profession_mask_id="herbalist",
        scene_state={"current_activity": "перебирает травы"},
    )


def test_safe_fallback_satisfies_required_phrase() -> None:
    packet = PlannerService.safe_packet(context(), "Нет подтверждённых данных.")
    response = ActorService.fallback(packet, context())
    assert OutputGuard().validate(response, packet).passed


def test_high_risk_commit_is_forced_to_gm_review() -> None:
    plan = PlannerPlan(
        intent_summary="Опасный квест",
        risk=Risk.HIGH,
        confidence=0.8,
        proposed_response_type=ResponseType.QUEST_OFFER,
        tool_requests=[
            ToolRequest(
                name="commit_quest",
                purpose="commit",
                arguments='{"title":"Тест","template_id":"COLLECT","objectives":[{"id":"o1","type":"COLLECT"}],"evidence":["k1"]}',
            )
        ],
    )
    requests = PlannerService._enforce_plan_risk(plan)
    assert '"gm_approval_required": true' in requests[0].arguments
