from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import CachedDecision
from faervell_npc.schemas import ActorPacket, ResponseType, SceneContext


class DecisionCacheService:
    SAFE_REUSABLE_TYPES = {
        ResponseType.DIALOGUE,
        ResponseType.LORE_ANSWER,
        ResponseType.MECHANICS_ANSWER,
        ResponseType.SAFE_UNKNOWN,
    }

    def fingerprint(self, player_message: str, context: SceneContext) -> str:
        normalized = re.sub(r"\s+", " ", player_message.casefold()).strip()
        payload = "|".join(
            [
                normalized,
                context.character_id,
                context.profession_mask_id,
                context.location_id or "",
                context.relationship_summary,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def get_approved(
        self,
        session: AsyncSession,
        player_message: str,
        context: SceneContext,
    ) -> ActorPacket | None:
        fingerprint = self.fingerprint(player_message, context)
        record = (
            await session.execute(
                select(CachedDecision).where(
                    CachedDecision.fingerprint == fingerprint,
                    CachedDecision.approved.is_(True),
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        packet = ActorPacket.model_validate(record.actor_packet_json)
        if packet.response_type not in self.SAFE_REUSABLE_TYPES:
            return None
        if any(key in packet.action_result for key in ("quest_id", "status", "inventory", "balance")):
            return None
        packet.scene_id = context.scene_id
        packet.player_name = context.player_name
        packet.profession_mask_id = context.profession_mask_id
        packet.location_name = context.location_name
        record.hit_count += 1
        return packet

    async def store_candidate(
        self,
        session: AsyncSession,
        player_message: str,
        context: SceneContext,
        packet: ActorPacket,
    ) -> str | None:
        if packet.response_type not in self.SAFE_REUSABLE_TYPES:
            return None
        if any(key in packet.action_result for key in ("quest_id", "status", "inventory", "balance")):
            return None
        fingerprint = self.fingerprint(player_message, context)
        record = (
            await session.execute(
                select(CachedDecision).where(CachedDecision.fingerprint == fingerprint)
            )
        ).scalar_one_or_none()
        data: dict[str, Any] = packet.model_dump(mode="json")
        if record is None:
            record = CachedDecision(
                fingerprint=fingerprint,
                route=packet.response_type.value,
                request_summary=player_message[:1000],
                actor_packet_json=data,
                approved=False,
            )
            session.add(record)
        else:
            record.route = packet.response_type.value
            record.request_summary = player_message[:1000]
            record.actor_packet_json = data
        return fingerprint
