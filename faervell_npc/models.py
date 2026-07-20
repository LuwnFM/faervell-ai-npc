from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from faervell_npc.config import get_settings
from faervell_npc.db import Base

settings = get_settings()


def uuid4_str() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceRevision(Base):
    __tablename__ = "source_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    source_id: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("source_id", "content_hash"),)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(String(500))
    section: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    corpus: Mapped[str] = mapped_column(String(32), index=True)
    access: Mapped[str] = mapped_column(String(64), index=True)
    disclosure_tier: Mapped[str] = mapped_column(String(32), index=True)
    disclosure_modes: Mapped[list[str]] = mapped_column(JSON, default=list)
    region_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    exact_values: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_knowledge_corpus_access", "corpus", "access"),
        Index(
            "ix_knowledge_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class SceneConfig(Base):
    __tablename__ = "scene_configs"

    channel_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scene_id: Mapped[str] = mapped_column(String(64), default=uuid4_str)
    location_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    profession_mask_id: Mapped[str] = mapped_column(String(64), default="traveler")
    category_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    category_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    location_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    automatic_appearance_allowed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    response_mode: Mapped[str] = mapped_column(String(32), default="MENTION_OR_REPLY")
    reply_hint_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    appearance_probability: Mapped[float] = mapped_column(
        Float, default=settings.traveler_default_appearance_probability
    )
    arrival_announcement_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TravelerPresence(Base):
    __tablename__ = "traveler_presence"

    traveler_entity_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default="traveler_01"
    )
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    current_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    current_scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_location_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    next_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    next_scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_location_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    next_location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    next_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_requested_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_priority: Mapped[float] = mapped_column(Float, default=0.0)
    cross_location_summons_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    event_locations_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    movement_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_scene_engaged_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TravelRequest(Base):
    __tablename__ = "traveler_travel_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    source_channel_id: Mapped[str] = mapped_column(String(32), index=True)
    target_scene_id: Mapped[str] = mapped_column(String(64), index=True)
    target_location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    requester_discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    source_message_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    content_excerpt: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    scheduled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CharacterBinding(Base):
    __tablename__ = "character_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    character_name: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("guild_id", "discord_user_id", "character_id"),)


class CharacterProfile(Base):
    __tablename__ = "character_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    source_channel_id: Mapped[str] = mapped_column(String(32), index=True)
    source_message_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    canonical_name: Mapped[str] = mapped_column(String(256), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    race: Mapped[str | None] = mapped_column(String(256), nullable=True)
    race_subtype: Mapped[str | None] = mapped_column(String(256), nullable=True)
    age_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    height_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    visible_profile: Mapped[str] = mapped_column(Text, default="")
    full_sheet: Mapped[str] = mapped_column(Text)
    attachment_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    sheet_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    identity_embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_character_owner_active", "guild_id", "owner_discord_user_id", "active"),
        Index(
            "ix_character_identity_embedding_hnsw",
            "identity_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"identity_embedding": "vector_cosine_ops"},
        ),
    )


class SceneCharacterIdentity(Base):
    __tablename__ = "scene_character_identities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), index=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    presented_name: Mapped[str] = mapped_column(String(256))
    presentation_text: Mapped[str] = mapped_column(Text, default="")
    match_status: Mapped[str] = mapped_column(String(32), index=True)
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_scene_identity_lookup", "scene_id", "discord_user_id", "active"),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    message_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    speaker_type: Mapped[str] = mapped_column(String(16), index=True)
    discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    character_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    traveler_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01")
    profession_mask_id: Mapped[str] = mapped_column(String(64), default="traveler")
    content: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(64), default="SCENE_PARTICIPANTS")
    referenced_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TravelerMemory(Base):
    __tablename__ = "traveler_character_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    holder_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01", index=True)
    observed_under_mask: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_type: Mapped[str] = mapped_column(String(64), index=True)
    perspective: Mapped[str] = mapped_column(String(32), index=True)
    statement: Mapped[str] = mapped_column(Text)
    trust_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED", index=True)
    npc_belief: Mapped[str] = mapped_column(String(32), default="UNCERTAIN")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source_message_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_referenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Memory v2 fields.  They are deliberately additive so existing 0.8 data can
    # be migrated in place without rewriting the append-only conversation log.
    traveler_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01", index=True)
    scope_type: Mapped[str] = mapped_column(String(32), default="PERSONAL", index=True)
    normalized_content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    speaker_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    speaker_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject_entity_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    participant_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    mentioned_dates: Mapped[list[str]] = mapped_column(JSON, default=list)
    novelty_score: Mapped[float] = mapped_column(Float, default=1.0)
    reinforcement_count: Mapped[int] = mapped_column(Integer, default=0)
    corroboration_count: Mapped[int] = mapped_column(Integer, default=0)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reinforced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_anchor: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_cherished: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    why_saved: Mapped[str] = mapped_column(Text, default="")
    attribution_mode: Mapped[str] = mapped_column(String(24), default="ATTRIBUTABLE")
    disclosure_scope: Mapped[str] = mapped_column(String(24), default="PUBLIC")
    confidentiality: Mapped[str] = mapped_column(String(24), default="PUBLIC")
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_quest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_location_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index(
            "ix_memory_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_memory_character_lifecycle", "character_id", "lifecycle_status"),
        Index("ix_memory_scope_lifecycle", "scope_type", "lifecycle_status"),
        Index("ix_memory_source_dedup", "source_message_id", "content_hash", "scope_type"),
    )


class MemoryClaim(Base):
    __tablename__ = "memory_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    traveler_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01", index=True)
    normalized_claim: Mapped[str] = mapped_column(Text)
    claim_hash: Mapped[str] = mapped_column(String(64), index=True)
    subject_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject_entity_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    claim_type: Mapped[str] = mapped_column(String(32), default="STATEMENT")
    current_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED", index=True)
    confirmation_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    contradiction_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reinforcement_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (Index("ix_claim_hash_status", "claim_hash", "current_status"),)


class MemoryEvidence(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    claim_id: Mapped[str] = mapped_column(ForeignKey("memory_claims.id", ondelete="CASCADE"), index=True)
    memory_id: Mapped[str | None] = mapped_column(
        ForeignKey("traveler_character_memories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(32), default="MESSAGE")
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    speaker_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    location_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_excerpt: Mapped[str] = mapped_column(Text, default="")
    trust_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED", index=True)
    attribution_mode: Mapped[str] = mapped_column(String(24), default="ATTRIBUTABLE")
    disclosure_scope: Mapped[str] = mapped_column(String(24), default="PUBLIC")
    heard_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("claim_id", "source_type", "source_id", name="uq_claim_evidence_source"),)


class TravelerCortexSnapshot(Base):
    __tablename__ = "traveler_cortex_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    traveler_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01", index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    identity_core: Mapped[str] = mapped_column(Text, default="")
    personal_memory_digest: Mapped[str] = mapped_column(Text, default="")
    relationship_digest: Mapped[str] = mapped_column(Text, default="")
    open_threads_digest: Mapped[str] = mapped_column(Text, default="")
    testimony_digest: Mapped[str] = mapped_column(Text, default="")
    shared_world_impressions: Mapped[str] = mapped_column(Text, default="")
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    dirty: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dirty_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("traveler_entity_id", "character_id", name="uq_cortex_character"),)


class TravelerOpenThread(Base):
    __tablename__ = "traveler_open_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    traveler_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01", index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    related_character_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(32), default="OPEN_QUESTION", index=True)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    related_claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    related_quest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class MemoryRelation(Base):
    __tablename__ = "memory_relations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    source_memory_id: Mapped[str] = mapped_column(String(36), index=True)
    target_memory_id: Mapped[str] = mapped_column(String(36), index=True)
    relation_type: Mapped[str] = mapped_column(String(32), index=True)
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(32), default="LOCAL")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_memory_id", "target_memory_id", "relation_type", name="uq_memory_relation"),
    )


class RelationshipState(Base):
    __tablename__ = "player_traveler_relations"

    character_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    familiarity: Mapped[float] = mapped_column(Float, default=0.0)
    trust: Mapped[float] = mapped_column(Float, default=0.0)
    respect: Mapped[float] = mapped_column(Float, default=0.0)
    wariness: Mapped[float] = mapped_column(Float, default=0.0)
    irritation: Mapped[float] = mapped_column(Float, default=0.0)
    reciprocity_balance: Mapped[int] = mapped_column(Integer, default=0)
    recognition_mode: Mapped[str] = mapped_column(String(32), default="SUBTLE_RECOGNITION")
    version: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="незнакомец")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class Quest(Base):
    __tablename__ = "quests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    issuer_entity_id: Mapped[str] = mapped_column(String(64), default="traveler_01")
    profession_mask_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    reward: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QuestObjective(Base):
    __tablename__ = "quest_objectives"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), index=True)
    objective_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipe_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")


class KnowledgeGap(Base):
    __tablename__ = "knowledge_gaps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    question: Mapped[str] = mapped_column(Text)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    profession_mask_id: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CachedDecision(Base):
    __tablename__ = "cached_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    route: Mapped[str] = mapped_column(String(32))
    request_summary: Mapped[str] = mapped_column(Text)
    actor_packet_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(160))
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (UniqueConstraint("action", "message_id", name="uq_audit_action_message"),)


class GuildRuntimeSettings(Base):
    __tablename__ = "guild_runtime_settings"

    guild_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    gm_review_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    regeneration_limit: Mapped[int] = mapped_column(Integer, default=1)
    model_footer_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    startup_lock_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enforce_startup_lock: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GMReviewRequest(Base):
    __tablename__ = "gm_review_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), index=True)
    player_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    request_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    related_quest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    gm_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResponseBundle(Base):
    __tablename__ = "response_bundles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    guild_id: Mapped[str] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), index=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    message_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    response_kind: Mapped[str] = mapped_column(String(32), default="DIALOGUE")
    model: Mapped[str] = mapped_column(String(160), default="local/template")
    model_history: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text, default="")
    actor_packet_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scene_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    regeneration_count: Mapped[int] = mapped_column(Integer, default=0)
    regeneration_limit: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ResponseFeedback(Base):
    __tablename__ = "response_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("response_bundles.id", ondelete="CASCADE"), index=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("bundle_id", "discord_user_id"),)


class KnowledgeImportRun(Base):
    __tablename__ = "knowledge_import_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    source_id: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    documents: Mapped[int] = mapped_column(Integer, default=0)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
