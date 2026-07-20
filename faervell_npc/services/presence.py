from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import SceneConfig, TravelerPresence, TravelRequest
from faervell_npc.schemas import IncomingMessage

_RANDOM_PING_RE = re.compile(
    r"^(?:тест|test|пинг|ping|бот|bot|проверка|чек|check|ку|кек|лол|lol|\+|[.!?]+)$",
    re.IGNORECASE,
)
_SUMMON_RE = re.compile(
    r"\b(?:странник|подойди|приди|приходи|появись|зайди|нужен|нужна|ищу|зову|позови|"
    r"услышь|ответь|помоги|работа|задание|квест)\b",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(
    r"\b(?:можешь|сможешь|хочу|нужно|надо|где|когда|почему|как|есть ли|расскажи|скажи|"
    r"поговорить|встретиться|вопрос)\b",
    re.IGNORECASE,
)
_OOC_RE = re.compile(r"(?:^|\s)(?:\(\(|//|\[ooc\]|ooc:)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PingAssessment:
    classification: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class PresenceTransition:
    previous_channel_id: str | None
    previous_location_name: str | None
    channel_id: str
    scene_id: str
    location_name: str | None
    profession_mask_id: str
    reply_hint_enabled: bool
    arrival_announcement_enabled: bool
    reason: str


class PresenceService:
    def __init__(self, *, rng: random.Random | None = None) -> None:
        self.settings = get_settings()
        self.rng = rng or random.SystemRandom()

    async def ensure_presence(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
    ) -> TravelerPresence:
        # Serialize all presence reads/updates. The bot is global to the guild,
        # so concurrent mentions from many channels must not create two active
        # locations or overwrite a higher-priority queued destination.
        presence = (
            await session.execute(
                select(TravelerPresence)
                .where(TravelerPresence.traveler_entity_id == "traveler_01")
                .with_for_update()
            )
        ).scalar_one_or_none()
        if presence is None:
            presence = TravelerPresence(
                traveler_entity_id="traveler_01",
                guild_id=guild_id,
                cross_location_summons_enabled=True,
            )
            session.add(presence)
            await session.flush()

        if presence.guild_id != guild_id:
            presence.guild_id = guild_id

        current_scene = None
        if presence.current_channel_id:
            current_scene = await session.get(SceneConfig, presence.current_channel_id)
            if current_scene is not None and not current_scene.enabled:
                current_scene = None

        if current_scene is None and not presence.movement_locked:
            self._clear_current(presence)

        startup_channel_id = self.settings.traveler_startup_lock_channel_id
        startup_lock_applies = (
            self.settings.traveler_enforce_startup_lock
            and startup_channel_id
            and not presence.startup_lock_released
            # A GM may deliberately add a second, manual lock after release.
            # Do not let the process-start policy overwrite that lock on the
            # next message; only an unlocked presence or the startup channel
            # itself is eligible for the startup reset.
            and (
                not presence.movement_locked
                or presence.locked_channel_id in {None, str(startup_channel_id)}
            )
        )
        if startup_lock_applies:
            presence.movement_locked = True
            presence.locked_channel_id = str(startup_channel_id)
            self._clear_next(presence)
        elif presence.movement_locked and presence.locked_channel_id is None:
            presence.movement_locked = False

        return presence

    @staticmethod
    def is_current_scene(presence: TravelerPresence, scene: SceneConfig) -> bool:
        return presence.current_channel_id == scene.channel_id

    def assess_cross_location_ping(
        self,
        content: str,
        *,
        mentioned: bool,
        replied_to_bot: bool,
    ) -> PingAssessment:
        text = re.sub(r"\s+", " ", content).strip()
        lowered = text.casefold()

        if not text:
            return PingAssessment("RANDOM", 0.0, "пустой пинг без содержательного обращения")

        score = 0.0
        reasons: list[str] = []

        if replied_to_bot:
            score += 0.52
            reasons.append("ответ на сообщение Странника")
        elif mentioned:
            score += 0.18
            reasons.append("прямое упоминание")

        if len(text) >= 18:
            score += 0.16
            reasons.append("содержательное сообщение")
        elif len(text) >= 8:
            score += 0.08

        if _SUMMON_RE.search(text):
            score += 0.30
            reasons.append("явное обращение или просьба прийти")

        if _REQUEST_RE.search(text) or "?" in text:
            score += 0.16
            reasons.append("вопрос или просьба")

        if any(marker in text for marker in ("*", "—", "«", "»")):
            score += 0.06
            reasons.append("RP-формат")

        if _RANDOM_PING_RE.fullmatch(lowered):
            score -= 0.75
            reasons.append("похоже на тестовый или случайный пинг")

        if _OOC_RE.search(text) and not _SUMMON_RE.search(text):
            score -= 0.22
            reasons.append("OOC-формат без призыва")

        if re.fullmatch(r"https?://\S+", text):
            score -= 0.35
            reasons.append("только ссылка")

        score = max(0.0, min(1.0, score))
        if score >= self.settings.traveler_cross_location_min_score:
            classification = "INTENTIONAL"
        elif score <= 0.25:
            classification = "RANDOM"
        else:
            classification = "AMBIGUOUS"

        return PingAssessment(
            classification=classification,
            score=round(score, 3),
            reason="; ".join(reasons) or "недостаточно признаков",
        )

    async def register_cross_location_ping(
        self,
        session: AsyncSession,
        *,
        scene: SceneConfig,
        incoming: IncomingMessage,
        mentioned: bool,
        replied_to_bot: bool,
        allow_scheduling: bool = True,
    ) -> PingAssessment:
        presence = await self.ensure_presence(session, guild_id=incoming.guild_id)
        assessment = self.assess_cross_location_ping(
            incoming.content,
            mentioned=mentioned,
            replied_to_bot=replied_to_bot,
        )

        existing = (
            await session.execute(
                select(TravelRequest).where(
                    TravelRequest.source_message_id == incoming.discord_message_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return PingAssessment(existing.classification, existing.score, existing.reason)

        scheduling_allowed = (
            presence.cross_location_summons_enabled
            and not presence.movement_locked
            and allow_scheduling
        )
        should_schedule = assessment.classification != "RANDOM" and scheduling_allowed
        priority = min(1.0, assessment.score + (0.12 if replied_to_bot else 0.0))
        scheduled_as_next = False

        if should_schedule and (
            presence.next_channel_id is None
            or presence.next_channel_id == scene.channel_id
            or priority >= presence.next_priority
        ):
            scheduled_as_next = True
            presence.next_channel_id = scene.channel_id
            presence.next_scene_id = scene.scene_id
            presence.next_location_id = scene.location_id
            presence.next_location_name = scene.location_name
            presence.next_reason = assessment.reason
            presence.next_source_message_id = incoming.discord_message_id
            presence.next_requested_by_discord_user_id = incoming.author_discord_id
            presence.next_priority = priority
            presence.next_planned_at = datetime.now(UTC)

        session.add(
            TravelRequest(
                guild_id=incoming.guild_id,
                source_channel_id=incoming.channel_id,
                target_scene_id=scene.scene_id,
                target_location_name=scene.location_name,
                requester_discord_user_id=incoming.author_discord_id,
                source_message_id=incoming.discord_message_id,
                content_excerpt=incoming.content[:1000],
                classification=assessment.classification,
                score=assessment.score,
                reason=assessment.reason,
                scheduled=scheduled_as_next,
            )
        )
        await session.flush()
        return assessment

    async def set_current_scene(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        scene: SceneConfig,
        reason: str,
    ) -> PresenceTransition:
        presence = await self.ensure_presence(session, guild_id=guild_id)
        if (
            presence.movement_locked
            and presence.locked_channel_id
            and presence.locked_channel_id != scene.channel_id
        ):
            raise ValueError(
                f"Странник заперт в канале {presence.locked_channel_id}; переход запрещён"
            )
        previous_channel_id = presence.current_channel_id
        previous_location_name = presence.current_location_name
        self._apply_current_scene(presence, scene)
        if presence.movement_locked:
            presence.locked_channel_id = scene.channel_id
        self._clear_next(presence)
        return PresenceTransition(
            previous_channel_id=previous_channel_id,
            previous_location_name=previous_location_name,
            channel_id=scene.channel_id,
            scene_id=scene.scene_id,
            location_name=scene.location_name,
            profession_mask_id=scene.profession_mask_id,
            reply_hint_enabled=scene.reply_hint_enabled,
            arrival_announcement_enabled=scene.arrival_announcement_enabled,
            reason=reason,
        )

    async def enforce_startup_lock(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        scene: SceneConfig,
    ) -> TravelerPresence:
        """Hard-reset presence to the configured test channel on every process start."""
        configured = self.settings.traveler_startup_lock_channel_id
        if configured and scene.channel_id != str(configured):
            raise ValueError("startup lock scene does not match configured channel")
        presence = await self.ensure_presence(session, guild_id=guild_id)
        presence.startup_lock_released = False
        presence.movement_locked = True
        presence.locked_channel_id = scene.channel_id
        self._apply_current_scene(presence, scene)
        self._clear_next(presence)
        return presence

    async def clear_destination(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
    ) -> TravelerPresence:
        presence = await self.ensure_presence(session, guild_id=guild_id)
        self._clear_next(presence)
        return presence

    async def touch_current_scene(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        channel_id: str,
    ) -> TravelerPresence:
        """Extend the current-scene lease after a real interaction.

        A queued travel request is a *plan*, never an interrupt. The movement
        loop checks this lease before consuming the queue, which keeps the
        Stranger in the current location while RP business is still active.
        """
        presence = await self.ensure_presence(session, guild_id=guild_id)
        if presence.current_channel_id == channel_id:
            now = datetime.now(UTC)
            presence.last_interaction_at = now
            presence.current_scene_engaged_until = now + timedelta(
                seconds=self.settings.traveler_scene_settle_seconds
            )
        return presence

    async def finish_current_scene(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
    ) -> TravelerPresence:
        """Release the scene lease without changing the queued destination."""
        presence = await self.ensure_presence(session, guild_id=guild_id)
        presence.current_scene_engaged_until = datetime.now(UTC)
        return presence

    async def set_summons_enabled(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        enabled: bool,
    ) -> TravelerPresence:
        presence = await self.ensure_presence(session, guild_id=guild_id)
        presence.cross_location_summons_enabled = enabled
        if not enabled:
            self._clear_next(presence)
        return presence

    async def set_event_locations_enabled(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        enabled: bool,
    ) -> TravelerPresence:
        presence = await self.ensure_presence(session, guild_id=guild_id)
        presence.event_locations_enabled = enabled
        return presence

    async def set_movement_lock(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        enabled: bool,
        scene: SceneConfig | None = None,
    ) -> TravelerPresence:
        presence = await self.ensure_presence(session, guild_id=guild_id)
        presence.movement_locked = enabled
        if enabled:
            if scene is None:
                raise ValueError("Для блокировки нужна сцена")
            # Re-locking the configured startup channel starts a new process
            # gate. A manual lock in any other location must remain a manual
            # lock and must not send the traveller back to bureaucracy.
            if scene.channel_id == str(self.settings.traveler_startup_lock_channel_id or ""):
                presence.startup_lock_released = False
            presence.locked_channel_id = scene.channel_id
            self._apply_current_scene(presence, scene)
            self._clear_next(presence)
        else:
            presence.startup_lock_released = True
            presence.locked_channel_id = None
        return presence

    async def release_startup_lock_and_roll(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        allowed_channel_ids: set[str] | None = None,
    ) -> PresenceTransition | None:
        """Release the per-process startup lock and immediately roll a destination.

        The release is persisted only for the current process lifetime. ``on_ready``
        resets it on every fresh start, so a crash or restart always returns the
        Stranger to the configured bureaucracy-domain channel.
        """

        presence = await self.ensure_presence(session, guild_id=guild_id)
        configured = self.settings.traveler_startup_lock_channel_id
        if configured is None or not self.settings.traveler_enforce_startup_lock:
            raise ValueError("Обязательный startup-lock не настроен")
        if presence.locked_channel_id not in {None, str(configured)}:
            raise ValueError("Сначала снимите ручную блокировку текущей локации")

        presence.startup_lock_released = True
        presence.movement_locked = False
        presence.locked_channel_id = None
        self._clear_next(presence)

        scenes = await self._automatic_scenes(
            session,
            guild_id=guild_id,
            current_channel_id=presence.current_channel_id,
            allowed_channel_ids=allowed_channel_ids,
        )
        if not scenes:
            return None
        target = self._weighted_choice(scenes)
        return await self.set_current_scene(
            session,
            guild_id=guild_id,
            scene=target,
            reason="выпуск из startup-lock: взвешенный выбор RP-локации",
        )

    async def tick(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        allowed_channel_ids: set[str] | None = None,
    ) -> PresenceTransition | None:
        if not self.settings.traveler_presence_enabled:
            return None

        presence = await self.ensure_presence(session, guild_id=guild_id)
        if presence.movement_locked:
            return None
        if (
            presence.current_scene_engaged_until is not None
            and presence.current_scene_engaged_until > datetime.now(UTC)
        ):
            return None

        target: SceneConfig | None = None
        reason = "случайное появление по вероятности локации"

        if presence.next_channel_id:
            queued = await session.get(SceneConfig, presence.next_channel_id)
            queued_allowed = (
                allowed_channel_ids is None or presence.next_channel_id in allowed_channel_ids
            )
            if queued is None or not queued.enabled or not queued_allowed:
                self._clear_next(presence)
            elif self.rng.random() <= self.settings.traveler_summon_move_chance:
                target = queued
                reason = "запланированный переход после осмысленного призыва"

        if target is None and presence.next_channel_id is None:
            scenes = await self._automatic_scenes(
                session,
                guild_id=guild_id,
                current_channel_id=presence.current_channel_id,
                allowed_channel_ids=allowed_channel_ids,
            )
            passed = [
                scene
                for scene in scenes
                if self.rng.random() <= max(0.0, min(1.0, scene.appearance_probability))
            ]
            if passed:
                target = self._weighted_choice(passed)

        if target is None:
            return None

        return await self.set_current_scene(
            session,
            guild_id=guild_id,
            scene=target,
            reason=reason,
        )

    @staticmethod
    async def _automatic_scenes(
        session: AsyncSession,
        *,
        guild_id: str,
        current_channel_id: str | None,
        allowed_channel_ids: set[str] | None,
    ) -> list[SceneConfig]:
        scenes = list(
            (
                await session.execute(
                    select(SceneConfig).where(
                        SceneConfig.guild_id == guild_id,
                        SceneConfig.enabled.is_(True),
                        SceneConfig.automatic_appearance_allowed.is_(True),
                        SceneConfig.channel_id != current_channel_id,
                        SceneConfig.appearance_probability > 0,
                    )
                )
            ).scalars()
        )
        if allowed_channel_ids is not None:
            scenes = [scene for scene in scenes if scene.channel_id in allowed_channel_ids]
        return scenes

    def _weighted_choice(self, scenes: list[SceneConfig]) -> SceneConfig:
        weights = [max(0.001, min(1.0, scene.appearance_probability)) for scene in scenes]
        total = sum(weights)
        pick = self.rng.random() * total
        running = 0.0
        for scene, weight in zip(scenes, weights, strict=True):
            running += weight
            if pick <= running:
                return scene
        return scenes[-1]

    @staticmethod
    def _apply_current_scene(presence: TravelerPresence, scene: SceneConfig) -> None:
        presence.current_channel_id = scene.channel_id
        presence.current_scene_id = scene.scene_id
        presence.current_location_id = scene.location_id
        presence.current_location_name = scene.location_name
        presence.arrived_at = datetime.now(UTC)
        presence.current_scene_engaged_until = None
        presence.last_interaction_at = None

    @staticmethod
    def _clear_current(presence: TravelerPresence) -> None:
        presence.current_channel_id = None
        presence.current_scene_id = None
        presence.current_location_id = None
        presence.current_location_name = None
        presence.arrived_at = None
        presence.current_scene_engaged_until = None
        presence.last_interaction_at = None

    @staticmethod
    def _clear_next(presence: TravelerPresence) -> None:
        presence.next_channel_id = None
        presence.next_scene_id = None
        presence.next_location_id = None
        presence.next_location_name = None
        presence.next_reason = None
        presence.next_source_message_id = None
        presence.next_requested_by_discord_user_id = None
        presence.next_priority = 0.0
        presence.next_planned_at = None
