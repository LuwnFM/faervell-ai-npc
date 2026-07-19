from __future__ import annotations

import asyncio
import json
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
    GMReviewRequest,
    GuildRuntimeSettings,
    KnowledgeGap,
    Quest,
    QuestObjective,
    ResponseBundle,
    ResponseFeedback,
    SceneConfig,
)
from faervell_npc.runtime import Runtime
from faervell_npc.schemas import ActorPacket, IncomingMessage, ProcessResult, SceneContext
from faervell_npc.services.behavior import BehaviorManager
from faervell_npc.services.characters import CharacterSheetParser
from faervell_npc.services.ingest import SourceIngestor
from faervell_npc.services.presence import PresenceTransition
from faervell_npc.services.stagecraft import arrival_activity


class ResponseFeedbackView(discord.ui.View):
    def __init__(self, bot: FaervellBot, bundle_id: str, *, regeneration_enabled: bool = True) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.bundle_id = bundle_id

        like = discord.ui.Button(  # type: ignore[var-annotated]
            label="Нравится",
            emoji="👍",
            style=discord.ButtonStyle.success,
            custom_id=f"stranger:like:{bundle_id}",
        )
        dislike = discord.ui.Button(  # type: ignore[var-annotated]
            label="Не нравится",
            emoji="👎",
            style=discord.ButtonStyle.danger,
            custom_id=f"stranger:dislike:{bundle_id}",
        )
        regenerate = discord.ui.Button(  # type: ignore[var-annotated]
            label="Перегенерировать",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id=f"stranger:regenerate:{bundle_id}",
            disabled=not regeneration_enabled,
        )

        async def rate_callback(interaction: discord.Interaction, rating: int) -> None:
            await self.bot.record_response_feedback(interaction, self.bundle_id, rating)

        async def like_callback(interaction: discord.Interaction) -> None:
            await rate_callback(interaction, 1)

        async def dislike_callback(interaction: discord.Interaction) -> None:
            await rate_callback(interaction, -1)

        async def regenerate_callback(interaction: discord.Interaction) -> None:
            await self.bot.regenerate_response(interaction, self.bundle_id)

        like.callback = like_callback  # type: ignore[method-assign]
        dislike.callback = dislike_callback  # type: ignore[method-assign]
        regenerate.callback = regenerate_callback  # type: ignore[method-assign]
        self.add_item(like)
        self.add_item(dislike)
        self.add_item(regenerate)


class GMReviewView(discord.ui.View):
    def __init__(self, bot: FaervellBot, review_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.review_id = review_id
        approve = discord.ui.Button(  # type: ignore[var-annotated]
            label="Одобрить",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"stranger:gm:approve:{review_id}",
        )
        reject = discord.ui.Button(  # type: ignore[var-annotated]
            label="Отклонить",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"stranger:gm:reject:{review_id}",
        )

        async def approve_callback(interaction: discord.Interaction) -> None:
            await self.bot.decide_gm_review(interaction, self.review_id, approved=True)

        async def reject_callback(interaction: discord.Interaction) -> None:
            await self.bot.decide_gm_review(interaction, self.review_id, approved=False)

        approve.callback = approve_callback  # type: ignore[method-assign]
        reject.callback = reject_callback  # type: ignore[method-assign]
        self.add_item(approve)
        self.add_item(reject)


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
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
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
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None or not scene.enabled:
                await interaction.response.send_message("Сначала включите сцену.", ephemeral=True)
                return
            try:
                await self.runtime.presence.set_current_scene(
                    session,
                    guild_id=str(interaction.guild_id),
                    scene=scene,
                    reason="ручное перемещение GM",
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await session.commit()
        await interaction.response.send_message(
            f"Странник теперь находится в локации **{scene.location_name or 'без названия'}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="appear_now",
        description="Немедленно показать видимый RP-пост появления в этом канале",
    )
    async def appear_now(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Появление возможно только в текстовом канале или ветке.", ephemeral=True
            )
            return
        writable, missing = self.bot._channel_postability(channel)
        if not writable:
            await interaction.response.send_message(
                "Странник видит канал, но не может полноценно писать здесь. "
                "Не хватает: " + ", ".join(missing),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with SessionLocal() as session:
            scene = await session.get(SceneConfig, str(interaction.channel_id))
            if scene is None:
                scene = SceneConfig(
                    channel_id=str(interaction.channel_id),
                    guild_id=str(interaction.guild_id),
                    enabled=True,
                    location_name=channel.name,
                    location_id=self._slug(channel.name),
                    profession_mask_id="traveler",
                )
                session.add(scene)
                await session.flush()
            else:
                scene.enabled = True
                if not scene.location_name:
                    scene.location_name = channel.name
                if not scene.location_id:
                    scene.location_id = self._slug(channel.name)
            try:
                transition = await self.runtime.presence.set_current_scene(
                    session,
                    guild_id=str(interaction.guild_id),
                    scene=scene,
                    reason="немедленное появление по команде GM",
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await session.commit()

        posted, error = await self.bot._announce_arrival(transition)
        if not posted:
            await interaction.followup.send(
                "Локация переключена, но RP-пост отправить не удалось: "
                + (error or "неизвестная ошибка"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Странник немедленно появился в локации **{scene.location_name}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="movement_lock",
        description="Временно запереть Странника в этом канале или снять ограничение",
    )
    async def movement_lock(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            scene = None
            if enabled:
                channel = interaction.channel
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    await interaction.response.send_message(
                        "Ограничение можно включить только в текстовом канале или ветке.",
                        ephemeral=True,
                    )
                    return
                writable, missing = self.bot._channel_postability(channel)
                if not writable:
                    await interaction.response.send_message(
                        "Нельзя закрепить Странника здесь. Не хватает: " + ", ".join(missing),
                        ephemeral=True,
                    )
                    return
                scene = await session.get(SceneConfig, str(interaction.channel_id))
                if scene is None:
                    scene = SceneConfig(
                        channel_id=str(interaction.channel_id),
                        guild_id=str(interaction.guild_id),
                        enabled=True,
                        location_name=channel.name,
                        location_id=self._slug(channel.name),
                        profession_mask_id="traveler",
                    )
                    session.add(scene)
                    await session.flush()
                else:
                    scene.enabled = True
            presence = await self.runtime.presence.set_movement_lock(
                session,
                guild_id=str(interaction.guild_id),
                enabled=enabled,
                scene=scene,
            )
            await session.commit()

        if enabled:
            text = (
                f"Странник закреплён в **{presence.current_location_name or 'этом канале'}**. "
                "Случайные переходы и призывы из других локаций не сработают."
            )
        elif presence.movement_locked:
            text = (
                "Ограничение не снято: включён обязательный тестовый startup-lock. "
                f"Странник остаётся в <#{presence.locked_channel_id}> до отключения "
                "TRAVELER_ENFORCE_STARTUP_LOCK и перезапуска."
            )
        else:
            text = "Ограничение снято. Странник снова может перемещаться между локациями."
        await interaction.response.send_message(text, ephemeral=True)

    @stranger.command(
        name="event_locations",
        description="Разрешить или запретить автоматические появления в категории ивентов",
    )
    async def event_locations(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            await self.runtime.presence.set_event_locations_enabled(
                session,
                guild_id=str(interaction.guild_id),
                enabled=enabled,
            )
            await session.commit()
        state = "разрешены" if enabled else "запрещены"
        await interaction.response.send_message(
            f"Автоматические появления в категории ивентов **{state}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="locations_sync",
        description="Синхронизировать RP-каналы из разрешённых категорий",
    )
    async def locations_sync(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.bot.sync_location_scenes(interaction.guild_id)
        await interaction.followup.send(
            "RP-каналы синхронизированы: "
            f"новых **{report['registered']}**, обновлено **{report['updated']}**, "
            f"пропущено без прав **{report['skipped_permissions']}**.",
            ephemeral=True,
        )

    @stranger.command(
        name="permissions",
        description="Проверить эффективные права Странника в этом канале",
    )
    async def permissions(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Проверка доступна только в текстовом канале или ветке.", ephemeral=True
            )
            return
        writable, missing = self.bot._channel_postability(channel)
        category_id = self.bot._channel_category_id(channel)
        if writable:
            state = "**может читать историю и писать RP-посты**"
        else:
            state = "**не может полноценно работать**; не хватает: " + ", ".join(missing)
        await interaction.response.send_message(
            f"Категория: `{category_id or 'нет'}`\nСтатус: {state}",
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
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
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
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            await self.runtime.presence.clear_destination(
                session,
                guild_id=str(interaction.guild_id),
            )
            await session.commit()
        await interaction.response.send_message("Следующая цель маршрута очищена.", ephemeral=True)

    @stranger.command(
        name="character_bind", description="Привязать активного RP-персонажа к аккаунту"
    )
    async def character_bind(
        self,
        interaction: discord.Interaction,
        character_name: str,
        character_id: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Команда работает только на сервере.", ephemeral=True
            )
            return
        async with SessionLocal() as session:
            existing = (
                (
                    await session.execute(
                        select(CharacterBinding).where(
                            CharacterBinding.guild_id == str(interaction.guild_id),
                            CharacterBinding.discord_user_id == str(interaction.user.id),
                            CharacterBinding.active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
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
            await interaction.response.send_message(
                "Команда работает только в канале.", ephemeral=True
            )
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

    @stranger.command(
        name="gm_channel",
        description="Назначить текущий канал для заявок на одобрение ГМ",
    )
    async def gm_channel(self, interaction: discord.Interaction) -> None:
        if not await self._require_gm(interaction):
            return
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with SessionLocal() as session:
            runtime = await session.get(GuildRuntimeSettings, str(interaction.guild_id))
            if runtime is None:
                runtime = GuildRuntimeSettings(guild_id=str(interaction.guild_id))
                session.add(runtime)
            runtime.gm_review_channel_id = str(interaction.channel_id)
            await session.commit()
        posted, failed, reasons = await self.bot._retry_pending_gm_reviews(
            guild_id=str(interaction.guild_id)
        )
        suffix = ""
        if reasons:
            suffix = "\nОшибки: " + "; ".join(reasons[:3])
        await interaction.followup.send(
            "Этот канал назначен служебным каналом заявок Странника. "
            f"Отправлено ожидающих заявок: **{posted}**, не отправлено: **{failed}**."
            + suffix,
            ephemeral=True,
        )

    @stranger.command(
        name="regeneration_limit",
        description="Задать число разрешённых перегенераций одного ответа",
    )
    async def regeneration_limit(self, interaction: discord.Interaction, uses: int) -> None:
        if not await self._require_gm(interaction):
            return
        uses = max(0, min(20, uses))
        if interaction.guild_id is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        async with SessionLocal() as session:
            runtime = await session.get(GuildRuntimeSettings, str(interaction.guild_id))
            if runtime is None:
                runtime = GuildRuntimeSettings(guild_id=str(interaction.guild_id))
                session.add(runtime)
            runtime.regeneration_limit = uses
            await session.commit()
        await interaction.response.send_message(
            f"Лимит перегенераций одного ответа: **{uses}**.", ephemeral=True
        )

    @stranger.command(
        name="startup_lock_status",
        description="Показать обязательный канал блокировки после запуска",
    )
    async def startup_lock_status(self, interaction: discord.Interaction) -> None:
        channel_id = self.settings.traveler_startup_lock_channel_id
        await interaction.response.send_message(
            "Блокировка при каждом запуске: "
            + (f"**включена**, канал <#{channel_id}>." if channel_id else "**выключена**."),
            ephemeral=True,
        )

    @stranger.command(
        name="knowledge_status",
        description="Проверить локальную базу знаний и Fandom API",
    )
    async def knowledge_status(
        self,
        interaction: discord.Interaction,
        probe_api: bool = False,
    ) -> None:
        if not await self._require_gm(interaction):
            return
        async with SessionLocal() as session:
            info = await self.runtime.knowledge.diagnostics(session)
        text = (
            f"Документов: **{info.documents}**, фрагментов: **{info.chunks}**, "
            f"страниц основной вики: **{info.wiki_documents}**.\n"
            f"Состояние: **{'готово' if info.healthy else 'нужен импорт'}** — `{info.reason}`.\n"
            f"Последний импорт: **{info.latest_run_status or 'не запускался'}**."
        )
        if info.latest_run_errors:
            text += "\nПервые ошибки: " + "; ".join(
                str(item.get("error") or item)[:140] for item in info.latest_run_errors[:3]
            )
        if not probe_api:
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ingestor = SourceIngestor()
        try:
            report = await ingestor.probe_fandom(
                "https://faervellrp.fandom.com/ru/wiki/FirewellRP_%D0%92%D0%B8%D0%BA%D0%B8",
                sample_title="Королевство Ивелтин",
            )
            continuation = report.get("continuation") or {}
            preview = str(report.get("sample_preview") or "").replace("`", "'")[:500]
            text += (
                f"\n\nAPI Fandom: **доступен**, сайт: **{report.get('site_name') or '—'}**."
                f"\nПродолжение списка страниц: **{'есть' if continuation else 'нет'}**."
                f"\nКонтрольная статья: **{report.get('sample_title') or '—'}**, "
                f"текста: **{report.get('sample_text_length') or 0}** символов."
                f"\n```text\n{preview}\n```"
            )
        except Exception as exc:
            text += f"\n\nAPI Fandom: **ошибка** — `{type(exc).__name__}: {str(exc)[:700]}`"
        finally:
            await ingestor.close()
        await interaction.followup.send(text[:1950], ephemeral=True)

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
            f"Ограничение одним каналом: **{'включено' if presence and presence.movement_locked else 'выключено'}**",
            f"Ивент-локации: **{'разрешены' if presence and presence.event_locations_enabled else 'выключены'}**",
            f"LLM: **{'включён' if self.settings.llm_enabled else 'локальный fallback'}**",
            f"Анкет персонажей: **{characters}**",
            f"Непроверенных пробелов знаний: **{gaps}**",
        ]
        channel = interaction.channel
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            writable, missing = self.bot._channel_postability(channel)
            automatic_scope = self.bot._channel_is_automatic_scope(
                channel,
                event_locations_enabled=bool(presence and presence.event_locations_enabled),
            )
            permission_text = "может писать" if writable else "не хватает: " + ", ".join(missing)
            lines.append(f"Права в канале: **{permission_text}**")
            lines.append(
                "Автоматическая локация: **"
                + ("да" if automatic_scope else "нет; доступна только вручную")
                + "**"
            )
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

    @stranger.command(
        name="behavior_scan", description="Экспортировать важные случаи для ручного патча"
    )
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
        self._locations_bootstrap_done = False
        self._knowledge_bootstrap_done = False
        self._views_restored = False
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

    @staticmethod
    def _channel_category_id(channel: discord.abc.GuildChannel | discord.Thread) -> int | None:
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            return parent.category_id if parent is not None else None
        return getattr(channel, "category_id", None)

    @staticmethod
    def _channel_postability(
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> tuple[bool, list[str]]:
        member = channel.guild.me
        if member is None:
            return False, ["бот не найден среди участников сервера"]
        permissions = channel.permissions_for(member)
        missing: list[str] = []
        if not permissions.view_channel:
            missing.append("Просматривать канал")
        if isinstance(channel, discord.Thread):
            if not permissions.send_messages_in_threads:
                missing.append("Отправлять сообщения в ветках")
        elif not permissions.send_messages:
            missing.append("Отправлять сообщения")
        if not permissions.read_message_history:
            missing.append("Читать историю сообщений")
        return not missing, missing

    def _channel_is_automatic_scope(
        self,
        channel: discord.abc.GuildChannel | discord.Thread,
        *,
        event_locations_enabled: bool,
    ) -> bool:
        category_id = self._channel_category_id(channel)
        if category_id in set(self.settings.traveler_rp_category_ids):
            return True
        return bool(
            event_locations_enabled
            and self.settings.traveler_events_category_id is not None
            and category_id == self.settings.traveler_events_category_id
        )

    def _location_channels(
        self,
        guild: discord.Guild,
    ) -> list[discord.TextChannel | discord.Thread]:
        channels: list[discord.TextChannel | discord.Thread] = list(guild.text_channels)
        seen = {channel.id for channel in channels}
        for thread in guild.threads:
            if thread.id not in seen:
                channels.append(thread)
                seen.add(thread.id)
        return channels

    def _automatic_destination_ids(
        self,
        guild: discord.Guild,
        *,
        event_locations_enabled: bool,
    ) -> set[str]:
        startup_lock = (
            str(self.settings.traveler_startup_lock_channel_id)
            if self.settings.traveler_enforce_startup_lock
            and self.settings.traveler_startup_lock_channel_id
            else None
        )
        if startup_lock:
            return {startup_lock}
        result: set[str] = set()
        for channel in self._location_channels(guild):
            if not self._channel_is_automatic_scope(
                channel,
                event_locations_enabled=event_locations_enabled,
            ):
                continue
            writable, _ = self._channel_postability(channel)
            if writable:
                result.add(str(channel.id))
        return result

    @staticmethod
    def _location_hierarchy(
        channel: discord.TextChannel | discord.Thread,
    ) -> tuple[str | None, str | None, str]:
        category = None
        parent_name = None
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            if parent is not None:
                parent_name = parent.name
                category = parent.category
        else:
            category = channel.category
        parts = [
            part
            for part in (
                category.name if category else None,
                parent_name,
                channel.name,
            )
            if part
        ]
        return (
            str(category.id) if category else None,
            category.name if category else None,
            " / ".join(parts),
        )

    async def sync_location_scenes(self, guild_id: int) -> dict[str, int]:
        guild = self.get_guild(guild_id)
        if guild is None:
            guild = await self.fetch_guild(guild_id)
        normal_categories = set(self.settings.traveler_rp_category_ids)
        manual_categories = set(self.settings.traveler_manual_only_category_ids)
        event_category = self.settings.traveler_events_category_id
        target_categories = normal_categories | manual_categories
        if event_category is not None:
            target_categories.add(event_category)

        registered = 0
        updated = 0
        skipped_permissions = 0
        startup_id = (
            str(self.settings.traveler_startup_lock_channel_id)
            if self.settings.traveler_startup_lock_channel_id
            else None
        )
        async with SessionLocal() as session:
            for channel in self._location_channels(guild):
                category_id = self._channel_category_id(channel)
                if category_id not in target_categories and str(channel.id) != startup_id:
                    continue
                writable, _ = self._channel_postability(channel)
                if not writable:
                    skipped_permissions += 1
                category_str, category_name, location_path = self._location_hierarchy(channel)
                automatic = bool(
                    writable
                    and (
                        category_id in normal_categories
                        or (event_category is not None and category_id == event_category)
                    )
                )
                if category_id in manual_categories:
                    automatic = False
                scene = await session.get(SceneConfig, str(channel.id))
                if scene is None:
                    scene = SceneConfig(
                        channel_id=str(channel.id),
                        guild_id=str(guild.id),
                        enabled=True,
                    )
                    session.add(scene)
                    registered += 1
                else:
                    updated += 1
                scene.enabled = True
                scene.location_id = re.sub(
                    r"[^a-zа-яё0-9]+", "_", location_path.casefold()
                ).strip("_")
                scene.location_name = channel.name
                scene.category_id = category_str
                scene.category_name = category_name
                scene.location_path = location_path
                scene.automatic_appearance_allowed = automatic
                if automatic and scene.appearance_probability <= 0:
                    scene.appearance_probability = (
                        self.settings.traveler_default_appearance_probability
                    )
                elif not automatic:
                    scene.appearance_probability = 0.0
            await session.commit()
        return {
            "registered": registered,
            "updated": updated,
            "skipped_permissions": skipped_permissions,
        }

    async def on_ready(self) -> None:
        print(f"Faervell Stranger logged in as {self.user} ({self.user.id if self.user else '?'})")
        if self.settings.discord_guild_id is not None:
            if self.settings.traveler_auto_register_locations and not self._locations_bootstrap_done:
                self._locations_bootstrap_done = True
                try:
                    report = await self.sync_location_scenes(self.settings.discord_guild_id)
                    print(
                        "RP location sync: "
                        f"registered={report['registered']} "
                        f"updated={report['updated']} "
                        f"unwritable={report['skipped_permissions']}"
                    )
                except Exception as exc:
                    print(f"RP location sync failed: {type(exc).__name__}: {exc}")

            # Test safety is not a soft preference: every process start resets the
            # traveller to the configured test channel and clears all queued travel.
            async with SessionLocal() as session:
                startup_id = self.settings.traveler_startup_lock_channel_id
                if self.settings.traveler_enforce_startup_lock and startup_id is not None:
                    startup_channel = self.get_channel(startup_id)
                    if startup_channel is None:
                        try:
                            startup_channel = await self.fetch_channel(startup_id)
                        except discord.HTTPException as exc:
                            raise RuntimeError(
                                f"Не удалось получить тестовый канал {startup_id}: {exc}"
                            ) from exc
                    if not isinstance(startup_channel, (discord.TextChannel, discord.Thread)):
                        raise RuntimeError("Тестовый канал блокировки не является текстовой локацией")
                    category_id, category_name, location_path = self._location_hierarchy(startup_channel)
                    scene = await session.get(SceneConfig, str(startup_id))
                    if scene is None:
                        scene = SceneConfig(
                            channel_id=str(startup_id),
                            guild_id=str(self.settings.discord_guild_id),
                        )
                        session.add(scene)
                    scene.enabled = True
                    scene.location_name = startup_channel.name
                    scene.location_id = re.sub(
                        r"[^a-zа-яё0-9]+", "_", location_path.casefold()
                    ).strip("_")
                    scene.category_id = category_id
                    scene.category_name = category_name
                    scene.location_path = location_path
                    scene.automatic_appearance_allowed = False
                    scene.appearance_probability = 0.0
                    await session.flush()
                    presence = await self.runtime.presence.enforce_startup_lock(
                        session,
                        guild_id=str(self.settings.discord_guild_id),
                        scene=scene,
                    )
                else:
                    presence = await self.runtime.presence.ensure_presence(
                        session,
                        guild_id=str(self.settings.discord_guild_id),
                    )
                await session.commit()
            print(
                "Traveler presence: "
                f"current={presence.current_location_name or 'none'} "
                f"channel={presence.current_channel_id or 'none'} "
                f"next={presence.next_location_name or 'none'} "
                f"locked={presence.movement_locked} "
                f"locked_channel={presence.locked_channel_id or 'none'}"
            )

        if not self._views_restored:
            self._views_restored = True
            await self._restore_persistent_views()

        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(
                self._presence_loop(), name="traveler-presence-loop"
            )

        if not self._knowledge_bootstrap_done and self.settings.knowledge_auto_ingest:
            self._knowledge_bootstrap_done = True
            asyncio.create_task(self._bootstrap_knowledge(), name="knowledge-bootstrap")

        if (
            not self._registry_bootstrap_done
            and self.settings.discord_character_registry_channel_id is not None
        ):
            self._registry_bootstrap_done = True
            asyncio.create_task(
                self._bootstrap_character_registry(), name="character-registry-bootstrap"
            )

    async def _bootstrap_knowledge(self) -> None:
        try:
            async with SessionLocal() as session:
                info = await self.runtime.knowledge.diagnostics(session)
            if info.healthy:
                print(
                    "Knowledge bootstrap skipped: "
                    f"wiki_documents={info.wiki_documents} chunks={info.chunks}"
                )
                return
            print(f"Knowledge bootstrap started: {info.reason}")
            ingestor = SourceIngestor()
            try:
                async with SessionLocal() as session:
                    report = await ingestor.ingest_manifest(session, Path("data/sources.yaml"))
                print(
                    "Knowledge bootstrap finished: "
                    f"documents={report['documents']} chunks={report['chunks']} "
                    f"errors={len(report['errors'])}"
                )
            finally:
                await ingestor.close()
        except Exception as exc:
            print(f"Knowledge bootstrap failed: {type(exc).__name__}: {exc}")

    async def _restore_persistent_views(self) -> None:
        async with SessionLocal() as session:
            bundles = list(
                (
                    await session.execute(
                        select(ResponseBundle)
                        .where(
                            ResponseBundle.active.is_(True),
                            ResponseBundle.last_message_id.is_not(None),
                        )
                        .order_by(ResponseBundle.updated_at.desc())
                        .limit(500)
                    )
                ).scalars()
            )
            reviews = list(
                (
                    await session.execute(
                        select(GMReviewRequest)
                        .where(
                            GMReviewRequest.status == "PENDING",
                            GMReviewRequest.gm_message_id.is_not(None),
                        )
                        .order_by(GMReviewRequest.created_at.desc())
                        .limit(200)
                    )
                ).scalars()
            )
            unposted_review_ids = list(
                (
                    await session.execute(
                        select(GMReviewRequest.id)
                        .where(
                            GMReviewRequest.status == "PENDING",
                            GMReviewRequest.gm_message_id.is_(None),
                        )
                        .order_by(GMReviewRequest.created_at.asc())
                        .limit(200)
                    )
                ).scalars()
            )
        for bundle in bundles:
            if bundle.last_message_id:
                self.add_view(
                    ResponseFeedbackView(
                        self,
                        bundle.id,
                        regeneration_enabled=bundle.response_kind != "ARRIVAL",
                    ),
                    message_id=int(bundle.last_message_id),
                )
        for review in reviews:
            if review.gm_message_id:
                self.add_view(GMReviewView(self, review.id), message_id=int(review.gm_message_id))
        restored_posted = 0
        restored_failed = 0
        for review_id in unposted_review_ids:
            posted, _ = await self._post_gm_review(review_id)
            if posted:
                restored_posted += 1
            else:
                restored_failed += 1
        print(
            "Persistent views restored: "
            f"responses={len(bundles)} gm_reviews={len(reviews)} "
            f"gm_reviews_retried={len(unposted_review_ids)} "
            f"gm_reviews_posted={restored_posted} gm_reviews_failed={restored_failed}"
        )

    async def record_response_feedback(
        self,
        interaction: discord.Interaction,
        bundle_id: str,
        rating: int,
    ) -> None:
        async with SessionLocal() as session:
            bundle = await session.get(ResponseBundle, bundle_id)
            if bundle is None or not bundle.active:
                await interaction.response.send_message("Этот ответ уже недоступен для оценки.", ephemeral=True)
                return
            existing = (
                await session.execute(
                    select(ResponseFeedback).where(
                        ResponseFeedback.bundle_id == bundle_id,
                        ResponseFeedback.discord_user_id == str(interaction.user.id),
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ResponseFeedback(
                        bundle_id=bundle_id,
                        discord_user_id=str(interaction.user.id),
                        rating=1 if rating > 0 else -1,
                    )
                )
            else:
                existing.rating = 1 if rating > 0 else -1
            await session.commit()
        await interaction.response.send_message(
            "Оценка сохранена: " + ("нравится" if rating > 0 else "не нравится") + ".",
            ephemeral=True,
        )

    async def regenerate_response(
        self,
        interaction: discord.Interaction,
        bundle_id: str,
    ) -> None:
        if not self._member_is_gm(interaction.user):
            await interaction.response.send_message(
                "Перегенерация доступна только администраторам и назначенным ГМ.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with SessionLocal() as session:
            bundle = await session.get(ResponseBundle, bundle_id)
            if bundle is None or not bundle.active:
                await interaction.followup.send("Ответ не найден или уже закрыт.", ephemeral=True)
                return
            if bundle.response_kind == "ARRIVAL":
                await interaction.followup.send("Шаблон появления не перегенерируется.", ephemeral=True)
                return
            if bundle.regeneration_count >= bundle.regeneration_limit:
                await interaction.followup.send("Лимит перегенераций исчерпан.", ephemeral=True)
                return
            packet = ActorPacket.model_validate(bundle.actor_packet_json)
            context = SceneContext.model_validate(bundle.scene_context_json)
            response, model, reason = await self.runtime.orchestrator.regenerate(
                session,
                packet=packet,
                context=context,
                excluded_models=set(bundle.model_history or []),
            )
            final_text = self._with_sources(response, list(bundle.citations_json or []))
            scene = await session.get(SceneConfig, bundle.channel_id)
            hint_enabled = bool(scene and scene.reply_hint_enabled)
            model_name = model or "local/template"
            parts = self._response_parts(final_text, enabled=hint_enabled, model=model_name)
            channel_id = int(bundle.channel_id)
            message_ids = list(bundle.message_ids or [])
            await session.commit()

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Канал ответа недоступен.", ephemeral=True)
            return

        old_messages: list[discord.Message] = []
        for message_id in message_ids:
            try:
                old_messages.append(await channel.fetch_message(int(message_id)))
            except discord.HTTPException:
                continue
        new_messages: list[discord.Message] = []
        view = ResponseFeedbackView(self, bundle_id, regeneration_enabled=True)
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            if index < len(old_messages):
                message = old_messages[index]
                await message.edit(content=part, view=view if is_last else None)
            else:
                if is_last:
                    message = await channel.send(part, view=view)
                else:
                    message = await channel.send(part)
            new_messages.append(message)
        for extra in old_messages[len(parts):]:
            try:
                await extra.delete()
            except discord.HTTPException:
                pass

        async with SessionLocal() as session:
            bundle = await session.get(ResponseBundle, bundle_id)
            if bundle is not None:
                bundle.message_ids = [str(item.id) for item in new_messages]
                bundle.last_message_id = str(new_messages[-1].id)
                bundle.content = final_text
                bundle.model = model_name
                bundle.model_history = [*(bundle.model_history or []), model_name]
                bundle.regeneration_count += 1
                await session.commit()
        await interaction.followup.send(
            f"Ответ перегенерирован моделью **{model_name}**. Причина выбора: `{reason or '—'}`.",
            ephemeral=True,
        )

    async def decide_gm_review(
        self,
        interaction: discord.Interaction,
        review_id: str,
        *,
        approved: bool,
    ) -> None:
        if not self._member_is_gm(interaction.user):
            await interaction.response.send_message("Решение доступно только ГМ.", ephemeral=True)
            return
        async with SessionLocal() as session:
            review = await session.get(GMReviewRequest, review_id)
            if review is None or review.status != "PENDING":
                await interaction.response.send_message("Заявка уже обработана.", ephemeral=True)
                return
            review.status = "APPROVED" if approved else "REJECTED"
            review.decided_by_discord_user_id = str(interaction.user.id)
            review.decided_at = datetime.now(UTC)
            quest_record: Quest | None = None
            quest_objectives: list[QuestObjective] = []
            if review.related_quest_id:
                quest_record = await session.get(Quest, review.related_quest_id)
                if quest_record is not None:
                    quest_record.status = "ACTIVE" if approved else "REJECTED"
                    quest_objectives = list(
                        (
                            await session.execute(
                                select(QuestObjective).where(
                                    QuestObjective.quest_id == quest_record.id
                                )
                            )
                        ).scalars()
                    )
            source_channel_id = review.channel_id
            quest_payload = dict((review.payload or {}).get("quest") or {})
            rp_decision_text = self._quest_decision_text(
                approved=approved,
                quest=quest_record,
                objectives=quest_objectives,
                fallback_payload=quest_payload,
            )
            await session.commit()
        await interaction.response.edit_message(
            content=(interaction.message.content if interaction.message else "")
            + f"\n\n**Решение:** {'одобрено' if approved else 'отклонено'} <@{interaction.user.id}>",
            view=None,
        )
        try:
            channel = self.get_channel(int(source_channel_id)) or await self.fetch_channel(
                int(source_channel_id)
            )
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await channel.send(
                    "*Странник ненадолго возвращается к разговору.*\n\n" + rp_decision_text
                )
        except discord.HTTPException:
            pass

    @staticmethod
    def _quest_decision_text(
        *,
        approved: bool,
        quest: Quest | None,
        objectives: list[QuestObjective],
        fallback_payload: dict[str, object],
    ) -> str:
        if not approved:
            return "— Это поручение пока не состоится. Я поищу другое дело."
        title = quest.title if quest is not None else str(fallback_payload.get("title") or "Поручение")
        constraints = dict(quest.constraints or {}) if quest is not None else fallback_payload
        description = str(constraints.get("description") or fallback_payload.get("description") or "").strip()
        location = str(constraints.get("location_name") or fallback_payload.get("location_name") or "").strip()
        reward = dict(quest.reward or {}) if quest is not None else {
            "amount": fallback_payload.get("reward_amount"),
            "currency_id": fallback_payload.get("reward_currency_id"),
        }
        lines = [f"— Условия ясны. Дело называется «{title}»."]
        if description:
            lines.append(description)
        if location:
            lines.append(f"Место выполнения: {location}.")
        if objectives:
            readable = {
                "DELIVER": "доставить пакет и получить подтверждение передачи",
                "FIND_LOCATION": "проверить путь и вернуться с описанием дороги",
                "INVESTIGATE": "осмотреть место и сообщить результаты",
                "ESCORT": "сопроводить путника до указанного места",
            }
            for objective in objectives[:3]:
                lines.append(
                    "Задача: " + readable.get(objective.objective_type, "выполнить поручение") + "."
                )
        amount = reward.get("amount")
        if isinstance(amount, (int, float, str)) and str(amount).strip():
            try:
                amount_text = f"{float(amount):g}"
            except ValueError:
                amount_text = str(amount)
            lines.append(
                f"Плата после выполнения — {amount_text} "
                f"{reward.get('currency_id') or 'местных монет'}."
            )
        lines.append("— Можешь отправляться, когда будешь готова.")
        return "\n".join(lines)

    async def _post_gm_review(self, review_id: str) -> tuple[bool, str]:
        async with SessionLocal() as session:
            review = await session.get(GMReviewRequest, review_id)
            if review is None:
                return False, "заявка не найдена"
            if review.status != "PENDING":
                return True, "заявка уже обработана"
            if review.gm_message_id:
                return True, "заявка уже опубликована"
            runtime = await session.get(GuildRuntimeSettings, review.guild_id)
            configured = (
                runtime.gm_review_channel_id if runtime and runtime.gm_review_channel_id else None
            ) or (
                str(self.settings.discord_gm_review_channel_id)
                if self.settings.discord_gm_review_channel_id
                else None
            ) or (
                str(self.settings.discord_admin_channel_id)
                if self.settings.discord_admin_channel_id
                else None
            )
            if not configured:
                print(f"GM review pending without channel: id={review.id} reason={review.reason}")
                return False, "служебный канал не назначен"
            payload = dict(review.payload or {})
            quest_payload = dict(payload.get("quest") or {})
            quest_lines: list[str] = []
            if quest_payload:
                quest_lines.append(f"Предлагаемое дело: **{quest_payload.get('title') or 'без названия'}**")
                if quest_payload.get("description"):
                    quest_lines.append(f"Описание: {quest_payload['description']}")
                if quest_payload.get("location_name"):
                    quest_lines.append(f"Место: {quest_payload['location_name']}")
                if quest_payload.get("reward_amount"):
                    quest_lines.append(
                        "Предлагаемая плата: "
                        f"{quest_payload['reward_amount']} "
                        f"{quest_payload.get('reward_currency_id') or 'местных монет'}"
                    )
            content = (
                f"**Новая заявка Странника: {review.request_type}**\n"
                f"ID: `{review.id}`\n"
                f"Причина: {review.reason}\n"
                f"Игрок: {f'<@{review.player_discord_user_id}>' if review.player_discord_user_id else 'не определён'}\n"
                f"Исходный канал: <#{review.channel_id}>\n"
                + ("\n".join(quest_lines) + "\n" if quest_lines else "")
                + f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)[:1200]}\n```"
            )
        try:
            channel_id = int(configured)
        except (TypeError, ValueError):
            print(f"Configured GM review channel id is invalid: {configured!r}")
            return False, "некорректный ID служебного канала"
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            print(
                "GM review channel fetch failed: "
                f"review={review_id} channel={configured} "
                f"error={type(exc).__name__}: {exc}"
            )
            return False, f"не удалось открыть канал: {type(exc).__name__}"
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            print(f"Configured GM review channel is not text: {configured}")
            return False, "назначенный объект не является текстовым каналом или веткой"

        if isinstance(channel, discord.Thread):
            try:
                if channel.archived:
                    await channel.edit(archived=False)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(
                    "GM review thread unarchive failed: "
                    f"review={review_id} channel={configured} "
                    f"error={type(exc).__name__}: {exc}"
                )
            try:
                await channel.join()
            except (discord.Forbidden, discord.HTTPException, discord.ClientException) as exc:
                print(
                    "GM review thread join skipped/failed: "
                    f"review={review_id} channel={configured} "
                    f"error={type(exc).__name__}: {exc}"
                )

        member = channel.guild.me
        if member is not None:
            permissions = channel.permissions_for(member)
            can_send = (
                permissions.send_messages_in_threads
                if isinstance(channel, discord.Thread)
                else permissions.send_messages
            )
            if not can_send:
                print(
                    "GM review post blocked by permissions: "
                    f"review={review_id} channel={configured} thread={isinstance(channel, discord.Thread)}"
                )
                return False, "у бота нет права отправлять сообщения в служебный канал"
        try:
            sent = await channel.send(
                content,
                view=GMReviewView(self, review_id),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            print(
                "GM review post failed: "
                f"review={review_id} channel={configured} "
                f"error={type(exc).__name__}: {exc}"
            )
            return False, f"Discord отклонил сообщение: {type(exc).__name__}"
        async with SessionLocal() as session:
            review = await session.get(GMReviewRequest, review_id)
            if review is not None:
                review.gm_message_id = str(sent.id)
                await session.commit()
        print(f"GM review posted: review={review_id} channel={configured} message={sent.id}")
        return True, "опубликовано"

    async def _retry_pending_gm_reviews(
        self,
        *,
        guild_id: str,
        scene_id: str | None = None,
        source_channel_id: str | None = None,
        limit: int = 100,
    ) -> tuple[int, int, list[str]]:
        conditions = [
            GMReviewRequest.guild_id == guild_id,
            GMReviewRequest.status == "PENDING",
            GMReviewRequest.gm_message_id.is_(None),
        ]
        if scene_id:
            conditions.append(GMReviewRequest.scene_id == scene_id)
        if source_channel_id:
            conditions.append(GMReviewRequest.channel_id == source_channel_id)
        async with SessionLocal() as session:
            review_ids = list(
                (
                    await session.execute(
                        select(GMReviewRequest.id)
                        .where(*conditions)
                        .order_by(GMReviewRequest.created_at.asc())
                        .limit(limit)
                    )
                ).scalars()
            )
        posted = 0
        failed = 0
        reasons: list[str] = []
        for review_id in review_ids:
            ok, reason = await self._post_gm_review(review_id)
            if ok:
                posted += 1
            else:
                failed += 1
                reasons.append(f"{review_id[:8]}: {reason}")
        return posted, failed, reasons

    async def _send_process_result(
        self,
        source_message: discord.Message,
        scene: SceneConfig,
        result: ProcessResult,
    ) -> None:
        final_text = self._with_sources(result.response, result.citations)
        model_name = result.used_actor_model or "local/template"
        guild_id = str(source_message.guild.id) if source_message.guild else "unknown"
        async with SessionLocal() as session:
            runtime = await session.get(GuildRuntimeSettings, guild_id)
            regen_limit = (
                runtime.regeneration_limit if runtime else self.settings.discord_regeneration_limit
            )
            bundle = ResponseBundle(
                guild_id=guild_id,
                channel_id=str(source_message.channel.id),
                scene_id=scene.scene_id,
                source_message_id=str(source_message.id),
                response_kind=result.actor_packet.response_type.value,
                model=model_name,
                model_history=[model_name],
                content=final_text,
                actor_packet_json=result.actor_packet.model_dump(mode="json"),
                scene_context_json=(
                    result.scene_context.model_dump(mode="json") if result.scene_context else {}
                ),
                citations_json=result.citations,
                regeneration_limit=regen_limit,
            )
            session.add(bundle)
            await session.flush()
            bundle_id = bundle.id
            await session.commit()

        parts = self._response_parts(final_text, enabled=scene.reply_hint_enabled, model=model_name)
        view = ResponseFeedbackView(self, bundle_id, regeneration_enabled=True)
        sent_messages: list[discord.Message] = []
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                sent = await source_message.reply(part, mention_author=False, view=view)
            else:
                sent = await source_message.reply(part, mention_author=False)
            sent_messages.append(sent)
            async with SessionLocal() as session:
                await self.runtime.orchestrator.record_outgoing(
                    session,
                    message_id=str(sent.id),
                    guild_id=guild_id,
                    channel_id=str(source_message.channel.id),
                    content=part,
                    created_at=sent.created_at,
                )
        async with SessionLocal() as session:
            stored_bundle = await session.get(ResponseBundle, bundle_id)
            if stored_bundle is not None:
                stored_bundle.message_ids = [str(item.id) for item in sent_messages]
                stored_bundle.last_message_id = str(sent_messages[-1].id)
                await session.commit()
        explicit_posted = False
        if result.gm_review_request_id:
            explicit_posted, reason = await self._post_gm_review(result.gm_review_request_id)
            if not explicit_posted:
                print(
                    "GM review explicit delivery failed: "
                    f"review={result.gm_review_request_id} reason={reason}"
                )
        posted, failed, reasons = await self._retry_pending_gm_reviews(
            guild_id=guild_id,
            scene_id=scene.scene_id,
            source_channel_id=str(source_message.channel.id),
            limit=20,
        )
        if posted or failed:
            print(
                "GM review delivery sweep: "
                f"guild={guild_id} scene={scene.scene_id} explicit={explicit_posted} "
                f"posted={posted} failed={failed} reasons={reasons[:3]}"
            )

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
            referenced_id = (
                str(message.reference.message_id) if message.reference.message_id else None
            )
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
            thread_id=str(message.channel.id)
            if isinstance(message.channel, discord.Thread)
            else None,
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
                    writable = False
                    automatic_scope = False
                    if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
                        writable, _ = self._channel_postability(message.channel)
                        automatic_scope = self._channel_is_automatic_scope(
                            message.channel,
                            event_locations_enabled=presence.event_locations_enabled,
                        )
                    assessment = await self.runtime.presence.register_cross_location_ping(
                        session,
                        scene=scene,
                        incoming=incoming,
                        mentioned=True,
                        replied_to_bot=False,
                        allow_scheduling=writable and automatic_scope,
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

            await self._send_process_result(message, scene, result)

    async def _presence_loop(self) -> None:
        await self.wait_until_ready()
        interval = max(30, self.settings.traveler_movement_interval_seconds)
        while not self.is_closed():
            try:
                await asyncio.sleep(interval)
                if self.settings.discord_guild_id is None:
                    continue
                guild = self.get_guild(self.settings.discord_guild_id)
                if guild is None:
                    continue
                async with SessionLocal() as session:
                    presence = await self.runtime.presence.ensure_presence(
                        session,
                        guild_id=str(self.settings.discord_guild_id),
                    )
                    allowed_channel_ids = self._automatic_destination_ids(
                        guild,
                        event_locations_enabled=presence.event_locations_enabled,
                    )
                    transition = await self.runtime.presence.tick(
                        session,
                        guild_id=str(self.settings.discord_guild_id),
                        allowed_channel_ids=allowed_channel_ids,
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
                    posted, error = await self._announce_arrival(transition)
                    if not posted:
                        print(
                            "Traveler arrival post skipped: "
                            f"channel={transition.channel_id} error={error or 'unknown'}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Traveler presence loop failed: {type(exc).__name__}: {exc}")

    async def _announce_arrival(
        self,
        transition: PresenceTransition,
    ) -> tuple[bool, str | None]:
        channel_id = int(transition.channel_id)
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"Arrival channel fetch failed: {error}")
                return False, error
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False, "цель не является текстовым каналом или веткой"

        writable, missing = self._channel_postability(channel)
        if not writable:
            error = "не хватает прав: " + ", ".join(missing)
            print(f"Arrival announcement blocked: channel={channel_id} {error}")
            return False, error

        text = self._arrival_text(
            profession_mask_id=transition.profession_mask_id,
            location_name=transition.location_name,
            scene_id=transition.scene_id,
        )
        model_name = "local/arrival-template"
        parts = self._response_parts(
            text,
            enabled=transition.reply_hint_enabled,
            model=model_name,
        )
        async with SessionLocal() as session:
            bundle = ResponseBundle(
                guild_id=str(self.settings.discord_guild_id),
                channel_id=str(channel_id),
                scene_id=transition.scene_id,
                response_kind="ARRIVAL",
                model=model_name,
                model_history=[model_name],
                content=text,
                actor_packet_json={},
                scene_context_json={},
                regeneration_limit=0,
            )
            session.add(bundle)
            await session.flush()
            bundle_id = bundle.id
            await session.commit()
        view = ResponseFeedbackView(self, bundle_id, regeneration_enabled=False)
        messages: list[discord.Message] = []
        for index, part in enumerate(parts):
            try:
                if index == len(parts) - 1:
                    sent = await channel.send(part, view=view)
                else:
                    sent = await channel.send(part)
            except discord.HTTPException as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"Arrival announcement failed: {error}")
                return False, error
            messages.append(sent)
            async with SessionLocal() as session:
                await self.runtime.orchestrator.record_outgoing(
                    session,
                    message_id=str(sent.id),
                    guild_id=str(self.settings.discord_guild_id),
                    channel_id=str(channel_id),
                    content=part,
                    created_at=sent.created_at,
                )
        async with SessionLocal() as session:
            stored_bundle = await session.get(ResponseBundle, bundle_id)
            if stored_bundle is not None:
                stored_bundle.message_ids = [str(item.id) for item in messages]
                stored_bundle.last_message_id = str(messages[-1].id)
                await session.commit()
        return True, None

    @staticmethod
    def _arrival_text(
        *, profession_mask_id: str, location_name: str | None, scene_id: str
    ) -> str:
        activity = arrival_activity(profession_mask_id, scene_id)
        place = f" в {location_name}" if location_name else ""
        return (
            f"*Через некоторое время Странник появляется{place}, {activity}. "
            "Он не вмешивается без приглашения, но остаётся неподалёку и осматривается.*"
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
                if (
                    suffix in {".txt", ".md", ".json", ".yaml", ".yml"}
                    and attachment.size <= 2_000_000
                ):
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
        # Raw links and English source identifiers break RP immersion. Keep only readable
        # Cyrillic titles; full URLs remain available in the audit log and feedback bundle.
        unique: list[str] = []
        for citation in citations:
            title = str(citation.get("title") or "").strip()
            if not title or re.search(r"[A-Za-z]", title):
                continue
            rendered = f"«{title}»"
            if rendered not in unique:
                unique.append(rendered)
        if not unique:
            return text
        return text + "\n\n-# Источники: " + " • ".join(unique[:4])

    def _response_parts(self, text: str, *, enabled: bool, model: str) -> list[str]:
        parts = self._split_message(text)
        suffixes: list[str] = []
        if enabled and self.settings.discord_reply_hint_text.strip():
            suffixes.append(f"||{self.settings.discord_reply_hint_text.strip()}||")
        if self.settings.discord_model_footer_enabled:
            suffixes.append(f"-# Модель: `{model}`")
        if not suffixes:
            return parts
        suffix = "\n\n" + "\n".join(suffixes)
        if len(parts[-1]) + len(suffix) <= 2000:
            parts[-1] += suffix
        else:
            parts.append("\n".join(suffixes))
        return parts

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
