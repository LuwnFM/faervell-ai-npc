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
    response_mode: Mapped[str] = mapped_column(String(32), default="MENTION_OR_REPLY")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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

    __table_args__ = (
        Index(
            "ix_memory_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
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
