from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from faervell_npc.config import get_settings
from faervell_npc.db import SessionLocal
from faervell_npc.models import CharacterBinding, KnowledgeGap, SceneConfig
from faervell_npc.runtime import Runtime
from faervell_npc.schemas import IncomingMessage
from faervell_npc.services.behavior import BehaviorManager
from faervell_npc.services.ingest import SourceIngestor


class StrangerCommands(commands.Cog):
    stranger = app_commands.Group(name="stranger", description="Управление ИИ-NPC Странником")

    def __init__(self, bot: FaervellBot, runtime: Runtime) -> None:
        self.bot = bot
        self.runtime = runtime
        self.settings = get_settings()
        self.behavior = BehaviorManager()

    def _is_gm(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            configured = set(self.settings.discord_gm_role_ids)
            return bool(configured.intersection(role.id for role in user.roles))
        return False

    async def _require_gm(self, interaction: discord.Interaction) -> bool:
        if self._is_gm(interaction):
            return True
        await interaction.response.send_message("Эта команда доступна только GM.", ephemeral=True)
        return False

    @stranger.command(name="scene_enable", description="Включить сцену Странника в этом канале")
    @app_commands.describe(location="Название локации", mask="Профессиональная маска")
    async def scene_enable(
        self,
        interaction: discord.Interaction,
        location: str,
        mask: str = "traveler",
    ) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                scene = SceneConfig(
                    channel_id=str(interaction.channel_id),
                    guild_id=str(interaction.guild_id),
                )
                session.add(scene)
            scene.enabled = True
            scene.location_name = location
            scene.location_id = self._slug(location)
            scene.profession_mask_id = mask
            await session.commit()
        await interaction.response.send_message(
            f"Сцена включена: **{location}**, маска: **{mask}**.", ephemeral=True
        )

    @stranger.command(name="scene_disable", description="Выключить сцену в этом канале")
    async def scene_disable(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene:
                scene.enabled = False
                await session.commit()
        await interaction.response.send_message("Сцена выключена.", ephemeral=True)

    @stranger.command(name="mask_set", description="Сменить профессиональную маску Странника")
    async def mask_set(self, interaction: discord.Interaction, mask: str) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if not scene:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            scene.profession_mask_id = mask
            await session.commit()
        await interaction.response.send_message(f"Текущая маска: **{mask}**.", ephemeral=True)

    @stranger.command(name="character_bind", description="Привязать активного RP-персонажа к аккаунту")
    async def character_bind(
        self,
        interaction: discord.Interaction,
        character_name: str,
        character_id: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            existing = (
                await session.execute(
                    select(CharacterBinding).where(
                        CharacterBinding.guild_id == str(interaction.guild_id),
                        CharacterBinding.discord_user_id == str(interaction.user.id),
                        CharacterBinding.active.is_(True),
                    )
                )
            ).scalars().all()
            for binding in existing:
                binding.active = False
            session.add(
                CharacterBinding(
                    guild_id=str(interaction.guild_id),
                    discord_user_id=str(interaction.user.id),
                    character_id=character_id,
                    character_name=character_name,
                    active=True,
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Активный персонаж: **{character_name}** (`{character_id}`).", ephemeral=True
        )

    @stranger.command(name="status", description="Показать состояние сцены и сервисов")
    async def status(self, interaction: discord.Interaction) -> None:
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            gaps = (
                await session.execute(
                    select(func.count(KnowledgeGap.id)).where(KnowledgeGap.status == "PENDING")
                )
            ).scalar_one()
        description = (
            f"Сцена: **{'включена' if scene and scene.enabled else 'выключена'}**\n"
            f"Локация: **{scene.location_name if scene else '—'}**\n"
            f"Маска: **{scene.profession_mask_id if scene else '—'}**\n"
            f"LLM: **{'включён' if self.settings.llm_enabled else 'локальный fallback'}**\n"
            f"Непроверенных пробелов знаний: **{gaps}**"
        )
        await interaction.response.send_message(description, ephemeral=True)

    @stranger.command(name="source_ingest", description="Переиндексировать источники проекта")
    async def source_ingest(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ingestor = SourceIngestor()
        try:
            async with SessionLocal() as session:
                report = await ingestor.ingest_manifest(session, Path("data/sources.yaml"))
            text = (
                f"Импорт завершён: документов **{report['documents']}**, "
                f"фрагментов **{report['chunks']}**, ошибок **{len(report['errors'])}**."
            )
            if report["errors"]:
                text += "\nПервые ошибки: " + "; ".join(
                    f"{item['id']}: {item['error'][:120]}" for item in report["errors"][:3]
                )
            await interaction.followup.send(text, ephemeral=True)
        finally:
            await ingestor.close()

    @stranger.command(name="behavior_scan", description="Экспортировать важные случаи для ручного патча")
    async def behavior_scan(self, interaction: discord.Interaction, days: int = 30) -> None:
        if not await self._require_gm(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with SessionLocal() as session:
            report = await self.behavior.scan(session, max(1, min(days, 365)))
        output = Path("data/exports") / f"behavior-scan-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
        self.behavior.export_scan(report, output)
        await interaction.followup.send(
            "Отчёт собран. Он ничего не применяет автоматически.",
            file=discord.File(output),
            ephemeral=True,
        )

    @staticmethod
    def _slug(text: str) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", "_", text.casefold()).strip("_")


class FaervellBot(commands.Bot):
    def __init__(self, runtime: Runtime) -> None:
        settings = get_settings()
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix=settings.discord_command_prefix, intents=intents)
        self.runtime = runtime
        self.settings = settings

    async def setup_hook(self) -> None:
        await self.add_cog(StrangerCommands(self, self.runtime))
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Faervell Stranger logged in as {self.user} ({self.user.id if self.user else '?'})")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.user is None:
            return

        mentioned = self.user in message.mentions
        replied_to_bot = False
        referenced_id: str | None = None
        if message.reference:
            referenced_id = str(message.reference.message_id) if message.reference.message_id else None
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message):
                replied_to_bot = resolved.author.id == self.user.id

        should_respond = mentioned or replied_to_bot
        channel_id = str(message.channel.id)
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, channel_id)
            if scene is None and not should_respond:
                return
            if scene is not None and not scene.enabled:
                return

        clean_content = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        incoming = IncomingMessage(
            discord_message_id=str(message.id),
            guild_id=str(message.guild.id),
            channel_id=channel_id,
            thread_id=str(message.channel.id) if isinstance(message.channel, discord.Thread) else None,
            author_discord_id=str(message.author.id),
            author_display_name=message.author.display_name,
            content=clean_content or message.content,
            created_at=message.created_at,
            is_gm=self._member_is_gm(message.author),
            referenced_message_id=referenced_id,
        )

        if not should_respond:
            async with SessionLocal() as session:
                await self.runtime.orchestrator.archive_only(session, incoming)
            return

        async with self.runtime.locks.lock(channel_id):
            async with message.channel.typing():
                async with SessionLocal() as session:
                    result = await self.runtime.orchestrator.process(session, incoming)

            final_text = self._with_sources(result.response, result.citations)
            for part in self._split_message(final_text):
                sent = await message.reply(part, mention_author=False)
                async with SessionLocal() as session:
                    await self.runtime.orchestrator.record_outgoing(
                        session,
                        message_id=str(sent.id),
                        guild_id=str(message.guild.id),
                        channel_id=channel_id,
                        content=part,
                        created_at=sent.created_at,
                    )

    def _member_is_gm(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        configured = set(self.settings.discord_gm_role_ids)
        return bool(configured.intersection(role.id for role in member.roles))

    @staticmethod
    def _with_sources(text: str, citations: list[dict[str, str | None]]) -> str:
        unique: list[str] = []
        for citation in citations:
            title = citation.get("title") or citation.get("source_id") or "источник"
            url = citation.get("url")
            rendered = f"[{title}](<{url}>)" if url else str(title)
            if rendered not in unique:
                unique.append(rendered)
        if not unique:
            return text
        return text + "\n\n-# Источники: " + " • ".join(unique[:4])

    @staticmethod
    def _split_message(text: str, limit: int = 1950) -> list[str]:
        if len(text) <= limit:
            return [text]
        parts: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = remaining.rfind(" ", 0, limit)
            if split_at <= 0:
                split_at = limit
            parts.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            parts.append(remaining)
        return parts
