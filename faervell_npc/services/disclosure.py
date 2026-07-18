from __future__ import annotations

from dataclasses import dataclass

from faervell_npc.schemas import (
    Corpus,
    DisclosureDecision,
    DisclosureExchange,
    DisclosureTier,
    KnowledgeHit,
)


@dataclass(slots=True)
class DisclosureContext:
    player_raised_topic: bool
    trust: float = 0.0
    reciprocity_balance: int = 0
    unlocked_knowledge_ids: set[str] | None = None


class LoreDisclosureEngine:
    def decide(self, hit: KnowledgeHit, context: DisclosureContext) -> DisclosureDecision:
        if hit.corpus == Corpus.MECHANICS:
            return DisclosureDecision(
                knowledge_id=hit.id,
                known=True,
                may_disclose=True,
                free_summary=hit.content,
                allowed_details=[hit.content],
                reason="MECHANICS_ALWAYS_FREE",
            )

        unlocked = context.unlocked_knowledge_ids or set()
        if hit.id in unlocked:
            return DisclosureDecision(
                knowledge_id=hit.id,
                known=True,
                may_disclose=True,
                free_summary=hit.content,
                allowed_details=[hit.content],
                reason="PREVIOUSLY_UNLOCKED",
            )

        if not context.player_raised_topic:
            return DisclosureDecision(
                knowledge_id=hit.id,
                known=True,
                may_disclose=False,
                free_summary="",
                withheld_details=[hit.content],
                required_exchange=DisclosureExchange(type="NONE"),
                reason="PLAYER_DID_NOT_RAISE_TOPIC",
            )

        tier = hit.disclosure_tier
        if tier == DisclosureTier.FREE:
            return DisclosureDecision(
                knowledge_id=hit.id,
                known=True,
                may_disclose=True,
                free_summary=hit.content,
                allowed_details=[hit.content],
                reason="PUBLIC_FREE_LORE",
            )

        summary = self._safe_summary(hit.content)
        if tier == DisclosureTier.USEFUL:
            if context.trust >= 0.65 or context.reciprocity_balance > 0:
                return DisclosureDecision(
                    knowledge_id=hit.id,
                    known=True,
                    may_disclose=True,
                    free_summary=hit.content,
                    allowed_details=[hit.content],
                    reason="RELATIONSHIP_UNLOCK",
                )
            exchange = DisclosureExchange(type="SERVICE", description="небольшая взаимная услуга")
        elif tier == DisclosureTier.VALUABLE:
            exchange = DisclosureExchange(
                type="QUEST", template="DELIVER_ITEM", difficulty="SMALL", description="небольшое поручение"
            )
        elif tier == DisclosureTier.RARE:
            exchange = DisclosureExchange(
                type="GM_APPROVAL", description="разрешение распорядителя игры или особое условие"
            )
        else:
            exchange = DisclosureExchange(type="GM_APPROVAL", description="сведения сейчас закрыты")

        return DisclosureDecision(
            knowledge_id=hit.id,
            known=True,
            may_disclose=False,
            free_summary=summary,
            withheld_details=[hit.content],
            required_exchange=exchange,
            reason=f"{tier.value}_KNOWLEDGE_REQUIRES_EXCHANGE",
        )

    @staticmethod
    def _safe_summary(content: str) -> str:
        first = content.strip().split(".", 1)[0].strip()
        if not first:
            return "Страннику знакома эта тема, но подробности он придержит."
        words = first.split()
        return " ".join(words[:24]) + ("…" if len(words) > 24 else ".")
