from faervell_npc.schemas import AccessClass, Corpus, DisclosureTier, KnowledgeHit
from faervell_npc.services.disclosure import DisclosureContext, LoreDisclosureEngine


def hit(corpus: Corpus, tier: DisclosureTier) -> KnowledgeHit:
    return KnowledgeHit(
        id="k1",
        source_id="wiki",
        title="Test",
        content="Точный вход находится у северного уступа.",
        corpus=corpus,
        access=AccessClass.PUBLIC_CANON,
        disclosure_tier=tier,
    )


def test_mechanics_is_always_free() -> None:
    decision = LoreDisclosureEngine().decide(
        hit(Corpus.MECHANICS, DisclosureTier.RESTRICTED),
        DisclosureContext(player_raised_topic=True),
    )
    assert decision.may_disclose is True
    assert decision.required_exchange.type == "NONE"


def test_valuable_lore_is_withheld() -> None:
    decision = LoreDisclosureEngine().decide(
        hit(Corpus.LORE, DisclosureTier.VALUABLE),
        DisclosureContext(player_raised_topic=True, trust=0.1),
    )
    assert decision.may_disclose is False
    assert decision.required_exchange.type == "QUEST"
    assert decision.withheld_details
