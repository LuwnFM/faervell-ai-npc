import pytest

from faervell_npc.services.quests import QuestService


class FakeQuest:
    id = "quest-1"
    status = "ACTIVE"
    constraints = {}


class FakeSession:
    def __init__(self, quest: FakeQuest | None) -> None:
        self.quest = quest

    async def get(self, _model: object, _quest_id: str) -> FakeQuest | None:
        return self.quest

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_quest_state_requires_server_verified_action_result() -> None:
    with pytest.raises(ValueError, match="server_verified"):
        await QuestService().advance(
            FakeSession(FakeQuest()),
            quest_id="quest-1",
            event="completed",
            action_result={},
        )


@pytest.mark.asyncio
async def test_quest_state_transition_is_explicit_and_audited() -> None:
    quest = FakeQuest()
    result = await QuestService().advance(
        FakeSession(quest),
        quest_id="quest-1",
        event="completed",
        action_result={"server_verified": True, "objective": "o1"},
    )
    assert result["previous_status"] == "ACTIVE"
    assert result["status"] == "COMPLETED"
    assert quest.constraints["last_event"] == "completed"
