from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import CharacterBinding, Quest, SceneConfig
from faervell_npc.schemas import IncomingMessage, SceneContext
from faervell_npc.services.memory import MemoryService


class SceneContextBuilder:
    def __init__(self, memory: MemoryService) -> None:
        self.memory = memory
        self.settings = get_settings()

    async def ensure_scene(
        self,
        session: AsyncSession,
        incoming: IncomingMessage,
    ) -> SceneConfig:
        scene = await session.get(SceneConfig, incoming.channel_id)
        if scene is None:
            scene = SceneConfig(
                channel_id=incoming.channel_id,
                guild_id=incoming.guild_id,
                enabled=True,
            )
            session.add(scene)
            await session.flush()
        return scene

    async def resolve_character(
        self,
        session: AsyncSession,
        incoming: IncomingMessage,
    ) -> tuple[str, str]:
        if incoming.character_id:
            return incoming.character_id, incoming.author_display_name
        binding = (
            await session.execute(
                select(CharacterBinding)
                .where(
                    CharacterBinding.guild_id == incoming.guild_id,
                    CharacterBinding.discord_user_id == incoming.author_discord_id,
                    CharacterBinding.active.is_(True),
                )
                .order_by(CharacterBinding.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if binding:
            return binding.character_id, binding.character_name
        digest = hashlib.blake2b(
            f"{self.settings.pseudonym_secret}:{incoming.guild_id}:{incoming.author_discord_id}".encode(),
            digest_size=12,
        ).hexdigest()
        return f"anon:{digest}", incoming.author_display_name

    async def build(
        self,
        session: AsyncSession,
        incoming: IncomingMessage,
        scene: SceneConfig,
        character_id: str,
        character_name: str,
    ) -> SceneContext:
        recent = await self.memory.recent_messages(session, scene.scene_id)
        memories = await self.memory.retrieve(
            session,
            character_id=character_id,
            query=incoming.content,
        )
        relationship = await self.memory.get_or_create_relationship(session, character_id)
        active_quests = (
            await session.execute(
                select(Quest).where(
                    Quest.character_id == character_id,
                    Quest.status.in_(["ACTIVE", "DRAFT", "PENDING_GM"]),
                )
            )
        ).scalars().all()
        return SceneContext(
            scene_id=scene.scene_id,
            location_id=scene.location_id,
            location_name=scene.location_name,
            profession_mask_id=scene.profession_mask_id,
            recognition_mode=relationship.recognition_mode,
            player_name=character_name,
            character_id=character_id,
            recent_messages=[
                {
                    "speaker": msg.speaker_type,
                    "content": msg.content,
                    "mask": msg.profession_mask_id,
                }
                for msg in recent
            ],
            memories=memories,
            active_quests=[
                {
                    "id": quest.id,
                    "title": quest.title,
                    "status": quest.status,
                    "template": quest.template_id,
                }
                for quest in active_quests
            ],
            relationship_summary=relationship.summary,
            scene_state={
                "mood": "спокойно-заинтересованный",
                "current_goal": "наблюдать за собеседником и продолжать своё занятие",
                "current_activity": self._activity_for_mask(scene.profession_mask_id),
            },
        )

    @staticmethod
    def _activity_for_mask(mask: str) -> str:
        return {
            "herbalist": "перебирает и связывает пучки трав",
            "artisan": "чинит потёртый ремень дорожной сумки",
            "merchant": "сверяет товар и мелкие гири",
            "guide": "ведёт ногтем по старой карте",
            "traveler": "очищает дорожную пряжку от пыли",
        }.get(mask, "занят простой дорожной работой")
