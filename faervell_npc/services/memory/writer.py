from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import (
    MemoryClaim,
    MemoryEvidence,
    TravelerCortexSnapshot,
    TravelerMemory,
    TravelerOpenThread,
)
from faervell_npc.services.embeddings import get_embedder

from .config import get_memory_config
from .deduplication import compare_claims
from .enums import LifecycleStatus, MemoryScope, MemoryTrust, OpenThreadKind, OpenThreadStatus
from .schemas import MemoryCandidate, MemoryWriteResult, TestimonyCandidate
from .text import content_hash, join_unique, normalize_text


class MemoryWriter:
    """Purely local write path inspired by Mimir's WriteMixin.

    It never calls an LLM. Claims are canonical records; messages and observations
    become evidence, so repeated rumours cannot silently become facts.
    """

    def __init__(self) -> None:
        self.embedder = get_embedder()

    async def record(self, session: AsyncSession, candidate: MemoryCandidate) -> MemoryWriteResult:
        now = candidate.created_at or datetime.now(UTC)
        normalized = normalize_text(candidate.normalized_claim or candidate.content)
        digest = content_hash(normalized)

        if candidate.source_message_id:
            duplicate = (
                await session.execute(
                    select(TravelerMemory)
                    .where(
                        TravelerMemory.source_message_id == candidate.source_message_id,
                        TravelerMemory.content_hash == digest,
                        TravelerMemory.scope_type == candidate.scope_type.value,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if duplicate:
                return MemoryWriteResult(
                    action="REJECTED",
                    memory_id=duplicate.id,
                    claim_id=duplicate.claim_id,
                    duplicate_of=duplicate.id,
                    reason="source_message_already_recorded",
                )

        claim_rows = (
            await session.execute(
                select(MemoryClaim)
                .where(MemoryClaim.traveler_entity_id == candidate.traveler_entity_id)
                .order_by(MemoryClaim.updated_at.desc())
                .limit(get_memory_config().candidate_pool)
            )
        ).scalars().all()
        claim = None
        decision = None
        for existing in claim_rows:
            candidate_decision = compare_claims(
                existing.normalized_claim,
                normalized,
                left_trust=existing.current_status,
                right_trust=candidate.trust_status.value,
                lexical_threshold=get_memory_config().dedup_lexical_threshold,
            )
            if candidate_decision.duplicate:
                claim = existing
                decision = candidate_decision
                break
            if candidate_decision.conflict:
                # Preserve incompatible versions as separate claims. The
                # relation graph can connect them later without collapsing
                # their evidence into a false consensus.
                decision = candidate_decision
        action = "CREATED"
        conflict = bool(decision and decision.conflict)
        if claim is None:
            action = "CONFLICT_CREATED" if conflict else "CREATED"
            claim = MemoryClaim(
                traveler_entity_id=candidate.traveler_entity_id,
                normalized_claim=normalized,
                claim_hash=content_hash(normalized),
                subject_character_ids=join_unique(candidate.subject_character_ids),
                subject_entity_keys=join_unique(candidate.subject_entity_keys),
                claim_type=candidate.scope_type.value,
                current_status=candidate.trust_status.value,
            )
            session.add(claim)
            await session.flush()
        else:
            action = "EVIDENCE_ADDED"
            claim.reinforcement_count += 1
            if candidate.speaker_character_id:
                speaker_rows = (
                    await session.execute(
                        select(MemoryEvidence.speaker_character_id).where(
                            MemoryEvidence.claim_id == claim.id,
                            MemoryEvidence.speaker_character_id.is_not(None),
                        )
                    )
                ).all()
                speakers = {str(row[0]) for row in speaker_rows if row[0]}
                if candidate.speaker_character_id not in speakers:
                    claim.corroboration_count += 1
            if claim.corroboration_count >= 2 and claim.current_status in {
                MemoryTrust.RUMOR.value,
                MemoryTrust.OTHER_CHARACTER_SAID.value,
            }:
                claim.current_status = MemoryTrust.CORROBORATED_RUMOR.value

        memory = TravelerMemory(
            character_id=candidate.owner_character_id or (candidate.subject_character_ids[0] if candidate.subject_character_ids else "world"),
            holder_entity_id=candidate.traveler_entity_id,
            traveler_entity_id=candidate.traveler_entity_id,
            memory_type=candidate.memory_type,
            perspective=candidate.trust_status.value,
            statement=candidate.content.strip(),
            trust_status=candidate.trust_status.value,
            npc_belief="CERTAIN" if candidate.trust_status == MemoryTrust.CONFIRMED else "UNCERTAIN",
            importance=candidate.importance,
            source_message_ids=[candidate.source_message_id] if candidate.source_message_id else [],
            embedding=self.embedder.embed(candidate.content),
            first_seen_at=now,
            scope_type=candidate.scope_type.value,
            normalized_content=normalized,
            content_hash=digest,
            claim_id=claim.id,
            speaker_character_id=candidate.speaker_character_id,
            speaker_display_name=candidate.speaker_display_name,
            subject_character_ids=join_unique(candidate.subject_character_ids),
            subject_entity_keys=join_unique(candidate.subject_entity_keys),
            participant_character_ids=join_unique(candidate.participant_character_ids),
            mentioned_dates=[item.isoformat() for item in candidate.mentioned_dates],
            novelty_score=1.0 if action == "CREATED" else 0.25,
            reinforcement_count=claim.reinforcement_count,
            corroboration_count=claim.corroboration_count,
            last_reinforced_at=now if action != "CREATED" else None,
            attribution_mode=candidate.attribution_mode.value,
            disclosure_scope=candidate.disclosure_scope.value,
            confidentiality=candidate.confidentiality.value,
            why_saved=candidate.why_saved,
            source_message_id=candidate.source_message_id,
            source_quest_id=candidate.source_quest_id,
            source_scene_id=candidate.scene_id,
            source_location_id=candidate.location_id,
            lifecycle_status=LifecycleStatus.ACTIVE.value,
        )
        session.add(memory)
        await session.flush()

        evidence = MemoryEvidence(
            claim_id=claim.id,
            memory_id=memory.id,
            source_type=candidate.source_type,
            source_id=candidate.source_id or candidate.source_message_id,
            source_message_id=candidate.source_message_id,
            speaker_character_id=candidate.speaker_character_id,
            scene_id=candidate.scene_id,
            location_id=candidate.location_id,
            source_excerpt=candidate.content[:2000],
            trust_status=candidate.trust_status.value,
            attribution_mode=candidate.attribution_mode.value,
            disclosure_scope=candidate.disclosure_scope.value,
            heard_at=now,
        )
        session.add(evidence)
        claim.reinforcement_count += 1 if action != "CREATED" else 0
        if candidate.scope_type in {MemoryScope.TESTIMONY, MemoryScope.WORLD_TESTIMONY, MemoryScope.RUMOR}:
            if claim.current_status == MemoryTrust.OTHER_CHARACTER_SAID.value:
                claim.current_status = MemoryTrust.OTHER_CHARACTER_SAID.value

        dirty_ids = await self.mark_cortex_dirty(
            session,
            [candidate.owner_character_id, *candidate.subject_character_ids, *candidate.participant_character_ids],
            reason=f"memory:{action.lower()}",
            traveler_entity_id=candidate.traveler_entity_id,
        )
        return MemoryWriteResult(
            action=action, memory_id=memory.id, claim_id=claim.id,
            cortex_character_ids_marked_dirty=dirty_ids,
            reason="conflicting evidence preserved" if conflict else "memory recorded",
        )

    async def record_testimony(self, session: AsyncSession, candidate: TestimonyCandidate) -> MemoryWriteResult:
        return await self.record(session, candidate)

    async def mark_cortex_dirty(
        self,
        session: AsyncSession,
        character_ids: list[str | None],
        *,
        reason: str,
        traveler_entity_id: str = "traveler_01",
    ) -> list[str]:
        result: list[str] = []
        for character_id in join_unique(item for item in character_ids if item):
            snapshot = (
                await session.execute(
                    select(TravelerCortexSnapshot).where(
                        TravelerCortexSnapshot.traveler_entity_id == traveler_entity_id,
                        TravelerCortexSnapshot.character_id == character_id,
                    )
                )
            ).scalar_one_or_none()
            if snapshot is None:
                snapshot = TravelerCortexSnapshot(
                    traveler_entity_id=traveler_entity_id, character_id=character_id,
                    dirty=True, dirty_reason=reason,
                )
                session.add(snapshot)
            else:
                snapshot.dirty = True
                snapshot.dirty_reason = reason
                snapshot.updated_at = datetime.now(UTC)
            result.append(character_id)
        return result

    async def create_open_thread(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        summary: str,
        kind: OpenThreadKind = OpenThreadKind.OPEN_QUESTION,
        importance: float = 0.5,
        source_memory_id: str | None = None,
        related_claim_id: str | None = None,
    ) -> TravelerOpenThread:
        thread = TravelerOpenThread(
            character_id=character_id, summary=summary, kind=kind.value,
            status=OpenThreadStatus.OPEN.value, importance=importance,
            source_memory_id=source_memory_id, related_claim_id=related_claim_id,
        )
        session.add(thread)
        await self.mark_cortex_dirty(session, [character_id], reason="open_thread:created")
        return thread

    async def resolve_open_thread(
        self,
        session: AsyncSession,
        thread_id: str,
        *,
        status: OpenThreadStatus = OpenThreadStatus.RESOLVED,
        resolution: str = "",
    ) -> TravelerOpenThread:
        thread = await session.get(TravelerOpenThread, thread_id)
        if thread is None:
            raise ValueError(f"unknown open thread: {thread_id}")
        thread.status = status.value
        thread.resolution = resolution[:2000] or None
        thread.resolved_at = datetime.now(UTC)
        thread.version += 1
        await self.mark_cortex_dirty(session, [thread.character_id], reason="open_thread:resolved")
        return thread

    async def anchor(self, session: AsyncSession, memory_id: str, value: bool = True) -> TravelerMemory:
        memory = await session.get(TravelerMemory, memory_id)
        if memory is None:
            raise ValueError(f"unknown memory: {memory_id}")
        memory.is_anchor = value
        await self.mark_cortex_dirty(session, [memory.character_id], reason="anchor:changed")
        return memory

    async def cherish(self, session: AsyncSession, memory_id: str, value: bool = True) -> TravelerMemory:
        memory = await session.get(TravelerMemory, memory_id)
        if memory is None:
            raise ValueError(f"unknown memory: {memory_id}")
        memory.is_cherished = value
        await self.mark_cortex_dirty(session, [memory.character_id], reason="cherished:changed")
        return memory

    async def update_importance(self, session: AsyncSession, memory_id: str, importance: float) -> TravelerMemory:
        memory = await session.get(TravelerMemory, memory_id)
        if memory is None:
            raise ValueError(f"unknown memory: {memory_id}")
        memory.importance = max(0.0, min(1.0, importance))
        await self.mark_cortex_dirty(session, [memory.character_id], reason="importance:changed")
        return memory
