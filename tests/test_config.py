from faervell_npc.config import Settings


def test_empty_optional_discord_ids_are_none() -> None:
    settings = Settings(discord_guild_id="", discord_admin_channel_id="")
    assert settings.discord_guild_id is None
    assert settings.discord_admin_channel_id is None


def test_empty_role_and_model_lists_parse_without_json() -> None:
    settings = Settings(
        discord_gm_role_ids="",
        actor_models="openrouter/free",
        planner_models="openai/gpt-5-nano,google/gemini-2.5-flash-lite",
    )
    assert settings.discord_gm_role_ids == []
    assert settings.actor_models == ["openrouter/free"]
    assert settings.planner_models == [
        "openai/gpt-5-nano",
        "google/gemini-2.5-flash-lite",
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
