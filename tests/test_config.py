from faervell_npc.config import Settings


def test_empty_optional_discord_ids_are_none() -> None:
    settings = Settings(discord_guild_id="", discord_admin_channel_id="")
    assert settings.discord_guild_id is None
    assert settings.discord_admin_channel_id is None


def test_empty_role_and_model_lists_parse_without_json() -> None:
    settings = Settings(
        discord_gm_role_ids="",
        actor_models="nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-120b",
        planner_models="openai/gpt-oss-120b:free,mistralai/ministral-14b-2512",
    )
    assert settings.discord_gm_role_ids == []
    assert settings.actor_models == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openai/gpt-oss-120b",
    ]
    assert settings.planner_models == [
        "openai/gpt-oss-120b:free",
        "mistralai/ministral-14b-2512",
    ]


def test_presence_defaults_are_safe() -> None:
    settings = Settings()
    assert settings.traveler_presence_enabled
    assert 0 <= settings.traveler_default_appearance_probability <= 1
    assert 0 <= settings.traveler_summon_move_chance <= 1
    assert settings.discord_reply_hint_text


def test_faervell_rp_category_defaults() -> None:
    settings = Settings()
    assert settings.traveler_auto_register_locations
    assert set(settings.traveler_rp_category_ids) == {
        682909341300293662,
        1057679719597879437,
        1133768572510941276,
        1255157727278403614,
        1426883198327193640,
        1057717821552984194,
        1459852302071631988,
    }
    assert settings.traveler_events_category_id == 1058403455934398495


def test_category_ids_parse_from_csv() -> None:
    settings = Settings(traveler_rp_category_ids="1,2,3")
    assert settings.traveler_rp_category_ids == [1, 2, 3]


def test_model_policy_blocks_random_router_and_rejected_models() -> None:
    settings = Settings(
        actor_models=(
            "openrouter/free,openai/gpt-oss-20b:free,"
            "nvidia/nemotron-nano-9b-v2:free,laguna-2.1-xs,"
            "openai/gpt-oss-120b:free,openai/gpt-oss-120b"
        )
    )
    assert settings.effective_actor_models == [
        "openai/gpt-oss-120b:free",
        "openai/gpt-oss-120b",
    ]


def test_model_policy_can_disable_all_paid_fallbacks() -> None:
    settings = Settings(
        actor_models="openai/gpt-oss-120b:free,openai/gpt-oss-120b",
        openrouter_allow_paid_fallback=False,
    )
    assert settings.effective_actor_models == ["openai/gpt-oss-120b:free"]


def test_openrouter_price_ceiling_defaults_to_twenty_cents_per_million() -> None:
    settings = Settings()
    assert settings.openrouter_max_prompt_price_per_million == 0.20
    assert settings.openrouter_max_completion_price_per_million == 0.20
    assert settings.openrouter_max_request_price_usd == 0.0
    assert "openrouter/free" not in settings.effective_actor_models


def test_deepseek_v4_flash_is_preferred_paid_planner() -> None:
    settings = Settings()
    assert settings.effective_planner_models[0] == "deepseek/deepseek-v4-flash"
    assert settings.effective_actor_models.index(
        "deepseek/deepseek-v4-flash"
    ) < settings.effective_actor_models.index("openai/gpt-oss-120b")
    assert settings.openrouter_planner_reasoning_effort == "high"


def test_planner_reasoning_effort_is_validated() -> None:
    settings = Settings(openrouter_planner_reasoning_effort="XHIGH")
    assert settings.openrouter_planner_reasoning_effort == "xhigh"
