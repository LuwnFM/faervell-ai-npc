from faervell_npc.config import Settings


def test_empty_optional_discord_ids_are_none() -> None:
    settings = Settings(discord_guild_id="", discord_admin_channel_id="")
    assert settings.discord_guild_id is None
    assert settings.discord_admin_channel_id is None
