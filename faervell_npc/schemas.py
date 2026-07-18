from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Route(StrEnum):
    CHAT = "CHAT"
    MECHANICS = "MECHANICS"
    LORE = "LORE"
    PLANNER = "PLANNER"


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Corpus(StrEnum):
    MECHANICS = "MECHANICS"
    LORE = "LORE"
    INTERNAL = "INTERNAL"


class AccessClass(StrEnum):
    PUBLIC_CANON = "PUBLIC_CANON"
    PUBLIC_GLOBAL_EVENT = "PUBLIC_GLOBAL_EVENT"
    PUBLIC_LOCAL_EVENT = "PUBLIC_LOCAL_EVENT"
    RUMOR = "RUMOR"
    TRAVELER_PRIVATE = "TRAVELER_PRIVATE"
    GM_SECRET = "GM_SECRET"
    UNTRUSTED_CLAIM = "UNTRUSTED_CLAIM"
    GM_ONLY = "GM_ONLY"


class DisclosureTier(StrEnum):
    FREE = "FREE"
    USEFUL = "USEFUL"
    VALUABLE = "VALUABLE"
    RARE = "RARE"
    RESTRICTED = "RESTRICTED"


class MemoryPerspective(StrEnum):
    FACT = "FACT"
    OBSERVED = "OBSERVED"
    PLAYER_SAID = "PLAYER_SAID"
    RUMOR = "RUMOR"
    INFERENCE = "INFERENCE"


class TrustStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    CORROBORATED = "CORROBORATED"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"
    REJECTED = "REJECTED"
    REDACTED = "REDACTED"


class ResponseType(StrEnum):
    DIALOGUE = "DIALOGUE"
    MECHANICS_ANSWER = "MECHANICS_ANSWER"
    LORE_ANSWER = "LORE_ANSWER"
    QUEST_OFFER = "QUEST_OFFER"
    QUEST_PROGRESS = "QUEST_PROGRESS"
    QUEST_COMPLETE = "QUEST_COMPLETE"
    SAFE_UNKNOWN = "SAFE_UNKNOWN"


class RouteDecision(BaseModel):
    route: Route
    reason: str
    risk: Risk = Risk.LOW
    needs_state_change: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class KnowledgeHit(BaseModel):
    id: str
    source_id: str
    title: str
    content: str
    corpus: Corpus
    access: AccessClass
    disclosure_tier: DisclosureTier
    disclosure_modes: list[str] = Field(default_factory=list)
    score: float = 0.0
    url: str | None = None
    revision: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryHit(BaseModel):
    id: str
    statement: str
    perspective: MemoryPerspective
    trust_status: TrustStatus
    importance: float
    source_message_ids: list[str]
    score: float = 0.0
    occurred_at: datetime | None = None


class DisclosureExchange(BaseModel):
    type: Literal["NONE", "COINS", "ITEM", "SERVICE", "QUEST", "TRUST", "GM_APPROVAL"]
    amount: float | None = None
    currency_id: str | None = None
    item_id: str | None = None
    template: str | None = None
    difficulty: str | None = None
    description: str | None = None


class DisclosureDecision(BaseModel):
    knowledge_id: str
    known: bool
    may_disclose: bool
    free_summary: str = ""
    allowed_details: list[str] = Field(default_factory=list)
    withheld_details: list[str] = Field(default_factory=list)
    required_exchange: DisclosureExchange = Field(default_factory=lambda: DisclosureExchange(type="NONE"))
    reason: str = ""


class ToolRequest(BaseModel):
    name: Literal[
        "search_lore",
        "search_mechanics",
        "get_world_weather",
        "get_market_price",
        "check_inventory",
        "create_quest_draft",
        "validate_quest",
        "commit_quest",
        "create_admin_question",
    ]
    arguments: str = "{}"
    purpose: str


class PlannerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_summary: str
    risk: Risk
    confidence: float = Field(ge=0.0, le=1.0)
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=6)
    proposed_response_type: ResponseType
    requires_gm_approval: bool = False
    gm_reason: str | None = None


class QuestObjectiveDraft(BaseModel):
    id: str
    type: Literal["COLLECT", "CRAFT", "DELIVER", "INVESTIGATE", "FIND_LOCATION", "REPAIR", "ESCORT"]
    entity_id: str | None = None
    recipe_id: str | None = None
    target_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=100)
    depends_on: list[str] = Field(default_factory=list)


class QuestDraft(BaseModel):
    title: str
    template_id: str
    objectives: list[QuestObjectiveDraft] = Field(min_length=1, max_length=8)
    reward_currency_id: str | None = None
    reward_amount: float = Field(default=0, ge=0)
    repeatable: bool = False
    gm_approval_required: bool = False
    evidence: list[str] = Field(default_factory=list)


class ActorPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    response_type: ResponseType
    traveler_entity_id: str = "traveler_01"
    profession_mask_id: str = "traveler"
    scene_id: str
    player_name: str
    location_name: str | None = None
    tone: list[str] = Field(default_factory=lambda: ["сдержанный", "наблюдательный"])
    facts_allowed: list[str] = Field(default_factory=list)
    facts_forbidden: list[str] = Field(default_factory=list)
    required_mentions: list[str] = Field(default_factory=list)
    memories_allowed: list[str] = Field(default_factory=list)
    action_result: dict[str, Any] = Field(default_factory=dict)
    quest_summary: QuestDraft | None = None
    disclosure_offer: DisclosureExchange | None = None
    player_raised_topic: bool = True
    max_length_words: int = Field(default=220, ge=30, le=350)
    ooc_note: str | None = None


class IncomingMessage(BaseModel):
    discord_message_id: str
    guild_id: str
    channel_id: str
    thread_id: str | None = None
    author_discord_id: str
    author_display_name: str
    content: str
    created_at: datetime
    character_id: str | None = None
    is_gm: bool = False
    referenced_message_id: str | None = None


class SceneContext(BaseModel):
    scene_id: str
    location_id: str | None = None
    location_name: str | None = None
    profession_mask_id: str = "traveler"
    recognition_mode: str = "SUBTLE_RECOGNITION"
    player_name: str
    character_id: str
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    memories: list[MemoryHit] = Field(default_factory=list)
    active_quests: list[dict[str, Any]] = Field(default_factory=list)
    relationship_summary: str = "незнакомец"
    scene_state: dict[str, Any] = Field(default_factory=dict)


class ProcessResult(BaseModel):
    route: RouteDecision
    response: str
    actor_packet: ActorPacket
    used_actor_model: str | None = None
    used_planner_model: str | None = None
    planner_escalated: bool = False
    guard_passed: bool = True
    citations: list[dict[str, str | None]] = Field(default_factory=list)
