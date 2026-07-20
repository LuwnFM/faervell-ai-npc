from faervell_npc.services.memory.cortex_renderer import CortexRenderer
from faervell_npc.services.memory.deduplication import compare_claims
from faervell_npc.services.memory.enums import MemoryScope, MemoryTrust
from faervell_npc.services.memory.schemas import CortexRenderBudget, MemoryRecallItem


def test_incompatible_trust_scopes_are_not_merged() -> None:
    assert compare_claims(
        "Лука был в городе",
        "Лука был в городе",
        left_trust=MemoryTrust.PLAYER_SAID.value,
        right_trust=MemoryTrust.CONFIRMED.value,
    ).conflict
    assert compare_claims(
        "В деревне пропадают люди",
        "В деревне пропадают люди",
        left_trust=MemoryTrust.OBSERVED.value,
        right_trust=MemoryTrust.RUMOR.value,
    ).conflict


def test_cortex_renderer_preserves_attribution_and_adapts_to_budget() -> None:
    item = MemoryRecallItem(
        id="m1",
        content="Лука связан с контрабандистами",
        scope_type=MemoryScope.TESTIMONY,
        trust_status=MemoryTrust.OTHER_CHARACTER_SAID,
        importance=0.7,
        speaker_name="Арден",
        actor_instruction="Назвать источник",
    )
    renderer = CortexRenderer()
    small = renderer.render(
        identity_core="Я Странник.",
        personal_memory_digest="",
        relationship_digest="осторожен",
        open_threads_digest="",
        testimony_digest="Арден говорил о Луке.",
        shared_world_impressions="",
        recalled_memories=[],
        recalled_testimonies=[item],
        budget=CortexRenderBudget(context_length=1024, reserved_output_tokens=900),
        snapshot_version=1,
    )
    large = renderer.render(
        identity_core="Я Странник.",
        personal_memory_digest="",
        relationship_digest="осторожен",
        open_threads_digest="",
        testimony_digest="Арден говорил о Луке.",
        shared_world_impressions="",
        recalled_memories=[],
        recalled_testimonies=[item],
        budget=CortexRenderBudget(context_length=8192, reserved_output_tokens=900),
        snapshot_version=1,
    )
    assert len(large.recalled_testimonies) >= len(small.recalled_testimonies)
    assert large.recalled_testimonies and large.recalled_testimonies[0].speaker_name == "Арден"
