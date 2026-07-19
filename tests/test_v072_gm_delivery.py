from __future__ import annotations

from types import SimpleNamespace

import pytest

from faervell_npc.schemas import ActorPacket, ResponseType, SceneContext
from faervell_npc.services.actor import ActorService
from faervell_npc.services.local_planner import LocalPlanner


class _FakeKnowledge:
    def __init__(self, hits: list[SimpleNamespace]) -> None:
        self.hits = hits

    async def search_world(self, *_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return self.hits


class _FakeTools:
    def __init__(self, hits: list[SimpleNamespace]) -> None:
        self.knowledge = _FakeKnowledge(hits)
        self.calls: list[str] = []

    async def execute(self, _session: object, request: object, **_kwargs: object) -> dict[str, object]:
        name = str(request.name)  # type: ignore[attr-defined]
        self.calls.append(name)
        if name == "commit_quest":
            return {
                "committed": True,
                "quest_id": "quest-1",
                "status": "PENDING_GM",
                "gm_review_request_id": "review-1",
            }
        if name == "create_gm_review":
            return {"status": "PENDING", "gm_review_request_id": "review-1"}
        raise AssertionError(name)


def _context() -> SceneContext:
    return SceneContext(
        scene_id="scene-1",
        guild_id="guild-1",
        channel_id="channel-1",
        location_id="domain",
        location_name="домен-бога-бюрократии",
        location_path="Анатор / домен-бога-бюрократии",
        player_name="Богиня",
        character_id="character-1",
        profession_mask_id="traveler",
    )


def test_actor_public_packet_removes_moderation_facts() -> None:
    packet = ActorPacket(
        response_type=ResponseType.DIALOGUE,
        scene_id="scene-1",
        player_name="Богиня",
        facts_allowed=[
            "Награду должен подтвердить ГМ.",
            "Мне нужно сперва уточнить условия этого дела.",
        ],
        action_result={"status": "PENDING_GM", "gm_review_request_id": "review-1"},
    )
    public = ActorService._public_packet(packet)
    joined = " ".join(public["facts_allowed"]).casefold()  # type: ignore[arg-type]
    assert "гм" not in joined
    assert "уточнить" in joined


def test_irrelevant_market_article_is_not_quest_evidence() -> None:
    assert not LocalPlanner._quest_evidence_is_relevant(
        location="Анатор / домен-бога-бюрократии",
        player_message="дай мне квест",
        title="Калькулятор рыночной цены",
        content="Таблица расчёта стоимости товаров и услуг.",
    )
    assert LocalPlanner._quest_evidence_is_relevant(
        location="Шегот / Гиблое Озеро",
        player_message="дай мне местное задание",
        title="Гиблое Озеро",
        content="Опасная местность Шегота, где пропадают путники.",
    )


@pytest.mark.asyncio
async def test_pending_local_quest_keeps_review_id_and_hides_gm() -> None:
    tools = _FakeTools(
        [
            SimpleNamespace(
                id="knowledge-1",
                source_id="wiki",
                title="Калькулятор рыночной цены",
                content="Таблица расчёта стоимости товаров.",
                corpus=SimpleNamespace(value="MECHANICS"),
                url=None,
            )
        ]
    )
    planner = LocalPlanner(tools)  # type: ignore[arg-type]
    packet = await planner._grounded_local_quest(
        object(),
        player_message="дай мне квест",
        context=_context(),
    )
    assert packet.action_result["gm_review_request_id"] == "review-1"
    assert tools.calls == ["commit_quest"]
    joined = " ".join(packet.facts_allowed).casefold()
    assert "гм" not in joined
    assert "заявк" not in joined
