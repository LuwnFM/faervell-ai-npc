from __future__ import annotations

from dataclasses import dataclass

from faervell_npc.config import get_settings


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    candidate_pool: int = 64
    lexical_weight: float = 0.22
    semantic_weight: float = 0.38
    subject_weight: float = 0.14
    importance_weight: float = 0.10
    trust_weight: float = 0.08
    recency_weight: float = 0.04
    anchor_weight: float = 0.12
    cherished_weight: float = 0.06
    dedup_lexical_threshold: float = 0.72
    dedup_vector_threshold: float = 0.92


def get_memory_config() -> MemoryConfig:
    settings = get_settings()
    return MemoryConfig(
        candidate_pool=settings.memory_candidate_pool,
        dedup_lexical_threshold=settings.memory_dedup_lexical_threshold,
        dedup_vector_threshold=settings.memory_dedup_vector_threshold,
    )
