from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from faervell_npc.config import get_settings
from faervell_npc.db import SessionLocal
from faervell_npc.models import (
    CharacterBinding,
    ConversationMessage,
    KnowledgeGap,
    SceneConfig,
)
from faervell_npc.runtime import Runtime
from faervell_npc.schemas import IncomingMessage
from faervell_npc.services.behavior import BehaviorManager
from faervell_npc.services.characters import CharacterSheetParser
from faervell_npc.services.ingest import SourceIngestor
from faervell_npc.services.presence import PresenceTransition


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
            await session.flush()
            presence = await self.runtime.presence.ensure_presence(
                session,
                guild_id=str(interaction.guild_id),
            )
            is_current = presence.current_channel_id == scene.channel_id
            await session.commit()
        suffix = " Сейчас Странник находится здесь." if is_current else ""
        await interaction.response.send_message(
            f"Сцена включена: **{location}**, маска: **{mask}**.{suffix}", ephemeral=True
        )

    @stranger.command(name="scene_disable", description="Выключить сцену в этом канале")
    async def scene_disable(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene:
                scene.enabled = False
                if interaction.guild_id is not None:
                    await session.flush()
                    await self.runtime.presence.ensure_presence(
                        session,
                        guild_id=str(interaction.guild_id),
                    )
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

    @stranger.command(
        name="reply_hint",
        description="Включить или выключить подсказку о пинге/ответе под постами",
    )
    async def reply_hint(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            scene.reply_hint_enabled = enabled
            await session.commit()
        state = "включена" if enabled else "выключена"
        await interaction.response.send_message(
            f"Подсказка под RP-постами **{state}** для этой локации.",
            ephemeral=True,
        )

    @stranger.command(
        name="appearance_chance",
        description="Задать вероятность появления Странника в этой локации за один цикл",
    )
    @app_commands.describe(percent="Вероятность от 0 до 100 процентов")
    async def appearance_chance(self, interaction: discord.Interaction, percent: float) -> None:
        if not await self._require_gm(interaction):
            return
        if not 0 <= percent <= 100:
            await interaction.response.send_message(
                "Вероятность должна быть от 0 до 100.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            scene.appearance_probability = percent / 100.0
            await session.commit()
        await interaction.response.send_message(
            f"Вероятность появления в этой локации: **{percent:.1f}%** за цикл.",
            ephemeral=True,
        )

    @stranger.command(
        name="arrival_announcements",
        description="Включить или выключить RP-пост при появлении в этой локации",
    )
    async def arrival_announcements(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            scene.arrival_announcement_enabled = enabled
            await session.commit()
        state = "включены" if enabled else "выключены"
        await interaction.response.send_message(
            f"Сообщения о появлении **{state}** для этой локации.", ephemeral=True
        )

    @stranger.command(name="move_here", description="Немедленно переместить Странника в этот канал")
    async def move_here(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None or not scene.enabled:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            await self.runtime.presence.set_current_scene(
                session,
                guild_id=str(interaction.guild_id),
                scene=scene,
                reason="ручное перемещение GM",
            )
            await session.commit()
        await interaction.response.send_message(
            f"Странник теперь находится в локации **{scene.location_name or 'без названия'}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="cross_location_summons",
        description="Разрешить или запретить планирование маршрута по пингам из других локаций",
    )
    async def cross_location_summons(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            await self.runtime.presence.set_summons_enabled(
                session,
                guild_id=str(interaction.guild_id),
                enabled=enabled,
            )
            await session.commit()
        state = "включено" if enabled else "выключено"
        await interaction.response.send_message(
            f"Планирование переходов по пингам из других локаций **{state}**.",
            ephemeral=True,
        )

    @stranger.command(name="travel_clear", description="Очистить следующую запланированную локацию")
    async def travel_clear(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            await self.runtime.presence.clear_destination(
                session,
                guild_id=str(interaction.guild_id),
            )
            await session.commit()
        await interaction.response.send_message("Следующая цель маршрута очищена.", ephemeral=True)

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

    @stranger.command(
        name="characters_sync",
        description="Загрузить анкеты персонажей из настроенного Discord-канала",
    )
    async def characters_sync(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        channel_id = self.settings.discord_character_registry_channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "Не задан DISCORD_CHARACTER_REGISTRY_CHANNEL_ID.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await self.bot.sync_character_registry(channel_id, full=True)
        except Exception as exc:
            await interaction.followup.send(
                f"Импорт анкет завершился ошибкой: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Анкеты синхронизированы: "
            f"найдено **{report['records']}**, загружено **{report['imported']}**, "
            f"пропущено **{report['skipped']}**, деактивировано **{report['deactivated']}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="identity_reset",
        description="Забыть выбранного персонажа в этой сцене и представиться заново",
    )
    async def identity_reset(self, interaction: discord.Interaction) -> None:
        if interaction.channel_id is None:
            await interaction.response.send_message("Команда работает только в канале.", ephemeral=True)
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                await interaction.response.send_message("В этом канале нет сцены.", ephemeral=True)
                return
            await self.runtime.characters.reset_identity(
                session,
                scene_id=scene.scene_id,
                discord_user_id=str(interaction.user.id),
            )
            await session.commit()
        await interaction.response.send_message(
            "Личность для этой сцены сброшена. Представьтесь Страннику снова.",
            ephemeral=True,
        )

    @stranger.command(
        name="commands_sync",
        description="Принудительно синхронизировать slash-команды на этом сервере",
    )
    async def commands_sync(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await self.bot.sync_application_commands(interaction.guild_id)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord не принял синхронизацию команд: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Slash-команды синхронизированы: **{count}**.", ephemeral=True
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
            characters = await self.runtime.characters.count_profiles(
                session,
                str(interaction.guild_id) if interaction.guild_id else None,
            )
            presence = None
            if interaction.guild_id is not None:
                presence = await self.runtime.presence.ensure_presence(
                    session,
                    guild_id=str(interaction.guild_id),
                )
                await session.commit()

        lines = [
            f"Сцена: **{'включена' if scene and scene.enabled else 'выключена'}**",
            f"Локация канала: **{scene.location_name if scene else '—'}**",
            f"Маска: **{scene.profession_mask_id if scene else '—'}**",
            f"Странник сейчас: **{presence.current_location_name if presence and presence.current_location_name else 'не появился'}**",
            f"Следующая цель: **{presence.next_location_name if presence and presence.next_location_name else 'не запланирована'}**",
            f"Переходы по пингам: **{'включены' if presence and presence.cross_location_summons_enabled else 'выключены'}**",
            f"LLM: **{'включён' if self.settings.llm_enabled else 'локальный fallback'}**",
            f"Анкет персонажей: **{characters}**",
            f"Непроверенных пробелов знаний: **{gaps}**",
        ]
        if scene is not None:
            lines.insert(
                4,
                f"Подсказка пинг/ответ: **{'включена' if scene.reply_hint_enabled else 'выключена'}**",
            )
            lines.insert(
                5,
                f"Шанс появления здесь: **{scene.appearance_probability * 100:.1f}%** за цикл",
            )
            lines.insert(
                6,
                f"Пост о появлении: **{'включён' if scene.arrival_announcement_enabled else 'выключен'}**",
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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


class EmergencyCommands(commands.Cog):
    def __init__(self, bot: FaervellBot) -> None:
        self.bot = bot

    @commands.command(name="stranger-sync")
    async def stranger_sync_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        """Emergency fallback when Discord has not shown slash commands yet."""
        if ctx.guild is None or not self.bot._member_is_gm(ctx.author):
            return
        try:
            count = await self.bot.sync_application_commands(ctx.guild.id)
        except discord.HTTPException as exc:
            await ctx.reply(
                f"Не удалось синхронизировать команды: `{type(exc).__name__}: {exc}`",
                mention_author=False,
            )
            return
        await ctx.reply(
            f"Slash-команды Странника синхронизированы: **{count}**.",
            mention_author=False,
        )


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
        self._registry_bootstrap_done = False
        self._presence_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(StrangerCommands(self, self.runtime))
        await self.add_cog(EmergencyCommands(self))
        count = await self.sync_application_commands(self.settings.discord_guild_id)
        scope = (
            f"guild={self.settings.discord_guild_id}"
            if self.settings.discord_guild_id is not None
            else "global"
        )
        print(f"Discord application commands synced: scope={scope} count={count}")

    async def sync_application_commands(self, guild_id: int | None) -> int:
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            # During MVP commands are copied to the configured guild so they appear immediately.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        return len(synced)

    async def on_ready(self) -> None:
        print(f"Faervell Stranger logged in as {self.user} ({self.user.id if self.user else '?'})")
        if self.settings.discord_guild_id is not None:
            async with SessionLocal() as session:
                presence = await self.runtime.presence.ensure_presence(
                    session,
                    guild_id=str(self.settings.discord_guild_id),
                )
                await session.commit()
            print(
                "Traveler presence: "
                f"current={presence.current_location_name or 'none'} "
                f"next={presence.next_location_name or 'none'}"
            )

        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(
                self._presence_loop(),
                name="traveler-presence-loop",
            )

        if (
            not self._registry_bootstrap_done
            and self.settings.discord_character_registry_channel_id is not None
        ):
            self._registry_bootstrap_done = True
            asyncio.create_task(self._bootstrap_character_registry(), name="character-registry-bootstrap")

    async def close(self) -> None:
        if self._presence_task is not None:
            self._presence_task.cancel()
            await asyncio.gather(self._presence_task, return_exceptions=True)
            self._presence_task = None
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.user is None:
            return

        await self.process_commands(message)
        if message.content.startswith(self.settings.discord_command_prefix):
            return

        mentioned = self.user in message.mentions
        replied_to_bot = False
        referenced_id: str | None = None
        referenced_created_at: datetime | None = None
        if message.reference:
            referenced_id = str(message.reference.message_id) if message.reference.message_id else None
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message):
                replied_to_bot = resolved.author.id == self.user.id
                if replied_to_bot:
                    referenced_created_at = resolved.created_at

        should_respond = mentioned or replied_to_bot
        channel_id = str(message.channel.id)
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, channel_id)
            if scene is None or not scene.enabled:
                return
            presence = await self.runtime.presence.ensure_presence(
                session,
                guild_id=str(message.guild.id),
            )
            is_current_location = self.runtime.presence.is_current_scene(presence, scene)
            if replied_to_bot and referenced_created_at is None and referenced_id is not None:
                archived_reference = await session.get(ConversationMessage, referenced_id)
                if archived_reference is not None and archived_reference.speaker_type == "NPC":
                    referenced_created_at = archived_reference.created_at
            reply_is_current_visit = self._reply_belongs_to_current_visit(
                referenced_created_at=referenced_created_at,
                arrived_at=presence.arrived_at,
            )
            await session.commit()

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

        if not is_current_location:
            async with SessionLocal() as session:
                assessment = None
                # A reply to an old post does not summon the Stranger after he has left.
                # Only a fresh direct mention in another registered location may plan travel.
                if mentioned and not replied_to_bot:
                    assessment = await self.runtime.presence.register_cross_location_ping(
                        session,
                        scene=scene,
                        incoming=incoming,
                        mentioned=True,
                        replied_to_bot=False,
                    )
                await self.runtime.orchestrator.archive_only(session, incoming)
            if replied_to_bot:
                print(
                    "Ignored stale reply outside current location: "
                    f"channel={scene.channel_id} message={incoming.discord_message_id}"
                )
            elif assessment is not None:
                print(
                    "Cross-location ping: "
                    f"location={scene.location_name or scene.channel_id} "
                    f"classification={assessment.classification} "
                    f"score={assessment.score:.3f}"
                )
            return

        if replied_to_bot and not reply_is_current_visit:
            async with SessionLocal() as session:
                await self.runtime.orchestrator.archive_only(session, incoming)
            print(
                "Ignored stale reply from a previous visit: "
                f"channel={scene.channel_id} message={incoming.discord_message_id}"
            )
            return

        if not should_respond:
            async with SessionLocal() as session:
                await self.runtime.orchestrator.archive_only(session, incoming)
            return

        async with self.runtime.locks.lock(channel_id):
            async with message.channel.typing():
                async with SessionLocal() as session:
                    result = await self.runtime.orchestrator.process(session, incoming)

            final_text = self._with_sources(result.response, result.citations)
            for part in self._reply_parts(final_text, enabled=scene.reply_hint_enabled):
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

    async def _presence_loop(self) -> None:
        await self.wait_until_ready()
        interval = max(30, self.settings.traveler_movement_interval_seconds)
        while not self.is_closed():
            try:
                await asyncio.sleep(interval)
                if self.settings.discord_guild_id is None:
                    continue
                async with SessionLocal() as session:
                    transition = await self.runtime.presence.tick(
                        session,
                        guild_id=str(self.settings.discord_guild_id),
                    )
                    await session.commit()
                if transition is None:
                    continue
                print(
                    "Traveler moved: "
                    f"from={transition.previous_location_name or 'none'} "
                    f"to={transition.location_name or transition.channel_id} "
                    f"reason={transition.reason}"
                )
                if transition.arrival_announcement_enabled:
                    await self._announce_arrival(transition)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Traveler presence loop failed: {type(exc).__name__}: {exc}")

    async def _announce_arrival(self, transition: PresenceTransition) -> None:
        channel_id = int(transition.channel_id)
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                print(f"Arrival channel fetch failed: {type(exc).__name__}: {exc}")
                return
        if not hasattr(channel, "send"):
            return

        text = self._arrival_text(
            profession_mask_id=transition.profession_mask_id,
            location_name=transition.location_name,
        )
        for part in self._reply_parts(
            text,
            enabled=transition.reply_hint_enabled,
        ):
            try:
                sent = await channel.send(part)  # type: ignore[union-attr]
            except discord.HTTPException as exc:
                print(f"Arrival announcement failed: {type(exc).__name__}: {exc}")
                return
            async with SessionLocal() as session:
                await self.runtime.orchestrator.record_outgoing(
                    session,
                    message_id=str(sent.id),
                    guild_id=str(self.settings.discord_guild_id),
                    channel_id=str(channel_id),
                    content=part,
                    created_at=sent.created_at,
                )

    @staticmethod
    def _arrival_text(*, profession_mask_id: str, location_name: str | None) -> str:
        activity = {
            "herbalist": "перебирая на ходу связку подсушенных трав",
            "artisan": "проверяя пальцем натяжение ремня на дорожной сумке",
            "merchant": "поправляя ремень тяжёлой сумы с товаром",
            "guide": "сверяя дорогу по потёртой карте",
            "traveler": "стряхивая с плаща дорожную пыль",
        }.get(profession_mask_id, "стряхивая с плаща дорожную пыль")
        place = f" в {location_name}" if location_name else ""
        return (
            f"*Через некоторое время Странник появляется{place}, {activity}. "
            "Он не торопится вмешиваться в чужие разговоры, но остаётся поблизости.*"
        )

    async def _bootstrap_character_registry(self) -> None:
        try:
            async with SessionLocal() as session:
                count = await self.runtime.characters.count_profiles(
                    session,
                    str(self.settings.discord_guild_id) if self.settings.discord_guild_id else None,
                )
            if count == 0 and self.settings.discord_character_registry_channel_id is not None:
                report = await self.sync_character_registry(
                    self.settings.discord_character_registry_channel_id,
                    full=True,
                )
                print(
                    "Character registry bootstrap: "
                    f"imported={report['imported']} skipped={report['skipped']}"
                )
        except Exception as exc:
            print(f"Character registry bootstrap failed: {type(exc).__name__}: {exc}")

    async def sync_character_registry(
        self,
        channel_id: int,
        *,
        full: bool,
    ) -> dict[str, int]:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise TypeError("Канал анкет должен быть текстовым каналом или веткой")
        if channel.guild.id != self.settings.discord_guild_id:
            raise ValueError("Канал анкет находится не на настроенном Discord-сервере")

        records: list[dict[str, object]] = []
        current: dict[str, object] | None = None

        async for message in channel.history(limit=None, oldest_first=True):
            text_parts = [message.content] if message.content else []
            attachment_urls: list[str] = []
            for attachment in message.attachments:
                attachment_urls.append(attachment.url)
                suffix = Path(attachment.filename).suffix.casefold()
                if suffix in {".txt", ".md", ".json", ".yaml", ".yml"} and attachment.size <= 2_000_000:
                    try:
                        payload = await attachment.read(use_cached=True)
                        text_parts.append(payload.decode("utf-8-sig", errors="replace"))
                    except discord.HTTPException:
                        pass
            message_text = "\n".join(part for part in text_parts if part).strip()
            parsed_start = CharacterSheetParser.parse(message_text)
            owner = message.mentions[0] if message.mentions else None

            if parsed_start is not None and owner is not None:
                if current is not None:
                    records.append(current)
                current = {
                    "guild_id": str(channel.guild.id),
                    "owner_id": str(owner.id),
                    "source_channel_id": str(channel.id),
                    "source_message_id": str(message.id),
                    "source_created_at": message.created_at,
                    "author_id": str(message.author.id),
                    "parts": [message_text],
                    "attachments": attachment_urls,
                }
                continue

            if current is not None and str(message.author.id) == current["author_id"]:
                if message_text:
                    parts = current["parts"]
                    assert isinstance(parts, list)
                    parts.append(message_text)
                attachments = current["attachments"]
                assert isinstance(attachments, list)
                attachments.extend(attachment_urls)

        if current is not None:
            records.append(current)

        imported = 0
        skipped = 0
        seen: set[str] = set()
        async with SessionLocal() as session:
            for record in records:
                source_message_id = str(record["source_message_id"])
                seen.add(source_message_id)
                parts = record["parts"]
                attachments = record["attachments"]
                assert isinstance(parts, list)
                assert isinstance(attachments, list)
                source_created_at = record["source_created_at"]
                if not isinstance(source_created_at, datetime):
                    source_created_at = None
                profile = await self.runtime.characters.upsert_profile(
                    session,
                    guild_id=str(record["guild_id"]),
                    owner_discord_user_id=str(record["owner_id"]),
                    source_channel_id=str(record["source_channel_id"]),
                    source_message_id=source_message_id,
                    source_created_at=source_created_at,
                    text="\n".join(str(part) for part in parts),
                    attachment_urls=[str(url) for url in attachments],
                )
                if profile is None:
                    skipped += 1
                else:
                    imported += 1
            deactivated = 0
            if full:
                deactivated = await self.runtime.characters.deactivate_missing(
                    session,
                    guild_id=str(channel.guild.id),
                    source_channel_id=str(channel.id),
                    seen_message_ids=seen,
                )
            await session.commit()
        return {
            "records": len(records),
            "imported": imported,
            "skipped": skipped,
            "deactivated": deactivated,
        }

    def _member_is_gm(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        configured = set(self.settings.discord_gm_role_ids)
        return bool(configured.intersection(role.id for role in member.roles))

    @staticmethod
    def _reply_belongs_to_current_visit(
        *,
        referenced_created_at: datetime | None,
        arrived_at: datetime | None,
    ) -> bool:
        if referenced_created_at is None or arrived_at is None:
            return False
        # Discord and PostgreSQL timestamps can differ by a fraction of a second.
        return referenced_created_at >= arrived_at - timedelta(seconds=2)

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

    def _reply_parts(self, text: str, *, enabled: bool) -> list[str]:
        hint = self.settings.discord_reply_hint_text.strip()
        if not enabled or not hint:
            return self._split_message(text)
        footer = f"\n\n||{hint}||"
        content_limit = max(500, 1950 - len(footer))
        parts = self._split_message(text, limit=content_limit)
        if not parts:
            return [footer.lstrip()]
        parts[-1] += footer
        return parts

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
