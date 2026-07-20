from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .enums import (
    AttributionMode,
    Confidentiality,
    DisclosureScope,
    LifecycleStatus,
    MemoryScope,
    MemoryTrust,
)

MemoryRoute = Literal["CHAT", "LORE", "MECHANICS", "PLANNER"]


class MemoryCandidate(BaseModel):
    traveler_entity_id: str = "traveler_01"
    owner_character_id: str | None = None
    scope_type: MemoryScope = MemoryScope.PERSONAL
    scene_id: str | None = None
    location_id: str | None = None
    content: str
    normalized_claim: str | None = None
    memory_type: str = "EVENT"
    trust_status: MemoryTrust = MemoryTrust.UNVERIFIED
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    speaker_character_id: str | None = None
    speaker_display_name: str | None = None
    subject_character_ids: list[str] = Field(default_factory=list)
    subject_entity_keys: list[str] = Field(default_factory=list)
    participant_character_ids: list[str] = Field(default_factory=list)
    source_type: str = "MESSAGE"
    source_id: str | None = None
    source_message_id: str | None = None
    source_quest_id: str | None = None
    mentioned_dates: list[date] = Field(default_factory=list)
    attribution_mode: AttributionMode = AttributionMode.ATTRIBUTABLE
    disclosure_scope: DisclosureScope = DisclosureScope.PUBLIC
    confidentiality: Confidentiality = Confidentiality.PUBLIC
    why_saved: str = ""
    created_at: datetime | None = None


class TestimonyCandidate(MemoryCandidate):
    scope_type: MemoryScope = MemoryScope.TESTIMONY
    speaker_character_id: str
    normalized_claim: str
    trust_status: MemoryTrust = MemoryTrust.OTHER_CHARACTER_SAID


class MemoryWriteResult(BaseModel):
    action: Literal["CREATED", "REINFORCED", "EVIDENCE_ADDED", "CONFLICT_CREATED", "REJECTED"]
    memory_id: str | None = None
    claim_id: str | None = None
    duplicate_of: str | None = None
    cortex_character_ids_marked_dirty: list[str] = Field(default_factory=list)
    reason: str = ""


class MemoryRecallQuery(BaseModel):
    traveler_entity_id: str = "traveler_01"
    active_character_id: str
    scene_id: str | None = None
    location_id: str | None = None
    text: str = ""
    route: MemoryRoute = "CHAT"
    active_quest_ids: list[str] = Field(default_factory=list)
    query_character_ids: list[str] = Field(default_factory=list)
    entity_keys: list[str] = Field(default_factory=list)
    mentioned_dates: list[date] = Field(default_factory=list)
    allow_testimonies: bool = True
    allow_world_rumors: bool = True


class MemoryRecallItem(BaseModel):
    id: str
    content: str
    normalized_claim: str | None = None
    scope_type: MemoryScope
    trust_status: MemoryTrust
    importance: float
    score: float = 0.0
    speaker_character_id: str | None = None
    speaker_name: str | None = None
    attribution_mode: AttributionMode = AttributionMode.ATTRIBUTABLE
    disclosure_scope: DisclosureScope = DisclosureScope.PUBLIC
    corroboration_count: int = 0
    source_message_id: str | None = None
    occurred_at: datetime | None = None
    confirmed: bool = False
    actor_instruction: str = ""


class CortexRenderBudget(BaseModel):
    model_id: str = "local/template"
    context_length: int = 8192
    reserved_output_tokens: int = 1000
    already_used_input_tokens: int = 0
    protocol_overhead: int = 256
    safety_margin: int = 256
    available_memory_tokens: int | None = None

    def usable_tokens(self, required_tokens: int = 0) -> int:
        available = self.context_length - self.reserved_output_tokens - self.protocol_overhead - self.safety_margin
        available -= self.already_used_input_tokens + required_tokens
        return max(0, self.available_memory_tokens if self.available_memory_tokens is not None else available)


class CortexContext(BaseModel):
    identity_core: str = ""
    personal_memory_digest: str = ""
    relationship_digest: str = ""
    open_threads_digest: str = ""
    testimony_digest: str = ""
    shared_world_impressions: str = ""
    recalled_memories: list[MemoryRecallItem] = Field(default_factory=list)
    recalled_testimonies: list[MemoryRecallItem] = Field(default_factory=list)
    snapshot_version: int = 0
    estimated_tokens: int = 0


class MemoryFilters(BaseModel):
    character_id: str | None = None
    scope_type: MemoryScope | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
