from faervell_npc.schemas import QuestDraft, QuestObjectiveDraft
from faervell_npc.services.rules import RuleEngine


def test_cycle_is_rejected() -> None:
    quest = QuestDraft(
        title="Плохой граф",
        template_id="COLLECT",
        objectives=[
            QuestObjectiveDraft(id="a", type="COLLECT", depends_on=["b"]),
            QuestObjectiveDraft(id="b", type="DELIVER", depends_on=["a"]),
        ],
        evidence=["wiki:test"],
    )
    result = RuleEngine().validate_quest(quest, "herbalist")
    assert not result.valid
    assert "objective_graph_has_cycle" in result.errors


def test_large_reward_is_rejected() -> None:
    quest = QuestDraft(
        title="Слишком щедро",
        template_id="COLLECT",
        objectives=[QuestObjectiveDraft(id="a", type="COLLECT")],
        reward_amount=100,
        evidence=["wiki:test"],
    )
    result = RuleEngine().validate_quest(quest, "herbalist")
    assert "reward_exceeds_small_quest_limit" in result.errors
