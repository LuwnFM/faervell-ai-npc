from discord.ext import commands

from faervell_npc.discord_bot import EmergencyCommands, StrangerCommands


def test_required_slash_commands_are_registered_in_group() -> None:
    names = {command.name for command in StrangerCommands.stranger.commands}
    assert {
        "scene_enable",
        "status",
        "characters_sync",
        "reply_hint",
        "appearance_chance",
        "cross_location_summons",
        "move_here",
        "commands_sync",
    }.issubset(names)


def test_emergency_prefix_sync_command_exists() -> None:
    assert isinstance(EmergencyCommands.stranger_sync_prefix, commands.Command)
    assert EmergencyCommands.stranger_sync_prefix.name == "stranger-sync"
