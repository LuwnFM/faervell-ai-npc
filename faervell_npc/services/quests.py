from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import Quest, QuestObjective, TravelerOpenThread


class QuestService:
    """Server-authoritative quest state machine.

    Dialogue templates can describe a state, but only a verified action result
    can move a committed quest through a state-changing event.
    """

    _EVENT_TO_STATUS = {
        "accepted": "ACTIVE",
        "progress_partial": "ACTIVE",
        "blocked": "BLOCKED",
        "completed": "COMPLETED",
        "declined": "DECLINED",
        "failed": "FAILED",
    }
    _ALLOWED = {
        "PENDING_GM": {"accepted", "declined"},
        "ACTIVE": {"progress_partial", "blocked", "completed", "declined", "failed"},
        "BLOCKED": {"progress_partial", "completed", "failed", "declined"},
    }
    _REQUIRES_VERIFIED_RESULT = {
        "accepted",
        "progress_partial",
        "blocked",
        "completed",
        "declined",
        "failed",
        "deliver_reward",
    }

    async def advance(
        self,
        session: AsyncSession,
        *,
        quest_id: str,
        event: str,
        action_result: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_event = event.strip().casefold()
        quest = await session.get(Quest, quest_id)
        if quest is None:
            raise ValueError("quest_not_found")
        if normalized_event not in self._REQUIRES_VERIFIED_RESULT:
            raise ValueError(f"unsupported_quest_event:{normalized_event}")
        if not bool(action_result.get("server_verified")):
            raise ValueError("quest_state_requires_server_verified_action_result")
        if normalized_event not in self._ALLOWED.get(quest.status, set()):
            raise ValueError(f"invalid_quest_transition:{quest.status}->{normalized_event}")

        previous_status = quest.status
        new_status = self._EVENT_TO_STATUS.get(normalized_event, quest.status)
        constraints = dict(quest.constraints or {})
        constraints["previous_status"] = previous_status
        constraints["last_event"] = normalized_event
        constraints["last_action_result"] = dict(action_result)
        constraints["last_transition_at"] = datetime.now(UTC).isoformat()
        quest.constraints = constraints
        quest.status = new_status
        for objective_id in action_result.get("completed_objective_ids") or []:
            key = str(objective_id)
            objective = await session.get(QuestObjective, key)
            if objective is None:
                objective = await session.get(QuestObjective, f"{quest.id}:{key}")
            if objective is not None:
                objective.status = "COMPLETED"
        if normalized_event in {"completed", "declined", "failed"}:
            open_threads: list[TravelerOpenThread] = []
            if hasattr(session, "execute"):
                open_threads = list((
                    await session.execute(
                        select(TravelerOpenThread).where(
                            TravelerOpenThread.related_quest_id == quest.id,
                            TravelerOpenThread.status == "OPEN",
                        )
                    )
                ).scalars().all())
            thread_status = {
                "completed": "RESOLVED",
                "declined": "CANCELLED",
                "failed": "BROKEN",
            }[normalized_event]
            for thread in open_threads:
                thread.status = thread_status
                thread.resolved_at = datetime.now(UTC)
                thread.resolution = f"quest:{normalized_event}"
                thread.version += 1
        await session.flush()
        return {
            "quest_id": quest.id,
            "previous_status": previous_status,
            "status": quest.status,
            "event": normalized_event,
            "server_verified": True,
        }
