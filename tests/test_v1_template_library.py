from pathlib import Path

from faervell_npc.runtime import build_runtime
from faervell_npc.services.template_library import TemplateLibrary


def test_template_library_audit_and_quest_archetypes_are_loaded() -> None:
    root = Path("behavior-pack/template-library")
    library = TemplateLibrary(root)
    records = library.all()

    assert len(records) == 512
    assert all(record.library_status != "REJECTED_PERSONA" for record in records)
    assert sum(record.requires_action_result for record in records) == 130
    assert library.is_quest_type_allowed("COLLECT_HERBS", "herbalist")
    assert library.choose_social("Кто ты и как тебя зовут?").id == "intro_name_001"
    assert library.choose_offer(
        player_message="Дай квест по сбору трав",
        profession_mask_id="herbalist",
        available_variables={"location_name", "quantity", "quest_title", "next_step"},
    ).quest_type == "COLLECT_HERBS"


def test_template_library_does_not_allow_action_template_as_other_event() -> None:
    library = TemplateLibrary(Path("behavior-pack/template-library"))
    assert library.is_quest_template_allowed("quest_collect_herbs_offer_01", "herbalist")
    assert not library.is_quest_template_allowed("quest_collect_herbs_completed_06", "herbalist")


def test_runtime_wires_economy_and_template_library() -> None:
    runtime = build_runtime()
    assert runtime.economy is not None
    assert runtime.templates is not None
