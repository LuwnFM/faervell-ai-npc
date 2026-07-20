from __future__ import annotations

from .schemas import CortexContext, CortexRenderBudget, MemoryRecallItem
from .text import compact_sentences, estimate_tokens


class CortexRenderer:
    """Deterministic, adaptive rendering; no fixed character cap and no LLM call."""

    def render(
        self,
        *,
        identity_core: str,
        personal_memory_digest: str,
        relationship_digest: str,
        open_threads_digest: str,
        testimony_digest: str,
        shared_world_impressions: str,
        recalled_memories: list[MemoryRecallItem],
        recalled_testimonies: list[MemoryRecallItem],
        budget: CortexRenderBudget,
        snapshot_version: int,
    ) -> CortexContext:
        required = "\n".join((identity_core, relationship_digest, open_threads_digest))
        available = budget.usable_tokens(estimate_tokens(required))
        # Preserve identity, active threads, and trust/attribution metadata first.
        personal = compact_sentences(personal_memory_digest, max(1, available // 3))
        testimony = compact_sentences(testimony_digest, max(1, available // 4))
        world = compact_sentences(shared_world_impressions, max(1, available // 6))
        memories = self._fit_items(recalled_memories, max(1, available // 3))
        testimonies = self._fit_items(recalled_testimonies, max(1, available // 3))
        text = "\n".join((identity_core, personal, relationship_digest, open_threads_digest, testimony, world))
        return CortexContext(
            identity_core=identity_core,
            personal_memory_digest=personal,
            relationship_digest=relationship_digest,
            open_threads_digest=open_threads_digest,
            testimony_digest=testimony,
            shared_world_impressions=world,
            recalled_memories=memories,
            recalled_testimonies=testimonies,
            snapshot_version=snapshot_version,
            estimated_tokens=estimate_tokens(text),
        )

    @staticmethod
    def _fit_items(items: list[MemoryRecallItem], budget: int) -> list[MemoryRecallItem]:
        selected: list[MemoryRecallItem] = []
        used = 0
        for item in items:
            line = item.content
            if item.trust_status.value not in {"CONFIRMED", "OBSERVED"}:
                line += f" [{item.trust_status.value}]"
            if item.speaker_name and item.attribution_mode.value == "ATTRIBUTABLE":
                line = f"{item.speaker_name}: {line}"
            cost = estimate_tokens(line)
            if selected and used + cost > budget:
                continue
            if used + cost <= budget:
                selected.append(item)
                used += cost
        return selected
