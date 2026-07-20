from __future__ import annotations

from datetime import UTC, datetime

from .config import get_memory_config
from .schemas import MemoryRecallItem


def rank_items(items: list[MemoryRecallItem], query: str) -> list[MemoryRecallItem]:
    config = get_memory_config()
    now = datetime.now(UTC)
    query_terms = set(query.casefold().split())
    for item in items:
        lexical = len(query_terms & set(item.content.casefold().split())) / max(1, len(query_terms))
        recency = 0.0
        if item.occurred_at:
            age_days = max(0.0, (now - item.occurred_at).total_seconds() / 86400)
            recency = 1.0 / (1.0 + age_days / 30.0)
        trust_bonus = 1.0 if item.trust_status.value == "CONFIRMED" else 0.45
        item.score = (
            config.lexical_weight * lexical
            + config.semantic_weight * item.score
            + config.importance_weight * item.importance
            + config.trust_weight * trust_bonus
            + config.recency_weight * recency
            + config.anchor_weight * (1.0 if item.id and item.confirmed else 0.0)
        )
    return sorted(items, key=lambda item: (-item.score, item.occurred_at or datetime.min.replace(tzinfo=UTC)))
