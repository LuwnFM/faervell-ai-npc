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
