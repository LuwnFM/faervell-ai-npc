from __future__ import annotations

from pathlib import Path

import pytest

from faervell_npc.schemas import AccessClass, Corpus, DisclosureTier, KnowledgeHit
from faervell_npc.services.retrieval_safety import (
    contamination_reasons,
    filter_and_rank,
    public_contamination_reasons,
    structured_lore_answer,
)
from faervell_npc.services.synonym_lexicon import SynonymLexicon


def _hit(
    *,
    hit_id: str,
    title: str,
    content: str,
    source_id: str = "faervell_wiki_root:page",
    score: float = 1.0,
    corpus: Corpus = Corpus.LORE,
) -> KnowledgeHit:
    return KnowledgeHit(
        id=hit_id,
        source_id=source_id,
        title=title,
        content=content,
        corpus=corpus,
        access=AccessClass.PUBLIC_CANON,
        disclosure_tier=DisclosureTier.FREE,
        score=score,
        url="https://faervellrp.fandom.com/ru/wiki/test",
        metadata={},
    )


@pytest.fixture(scope="module")
def lexicon() -> SynonymLexicon:
    path = Path(__file__).resolve().parents[1] / "data" / "synonyms" / "russian_synonyms.sqlite3"
    service = SynonymLexicon(path)
    assert service.available
    return service


@pytest.mark.parametrize(
    "query",
    [
        "Кто король Ивелтина?",
        "Кто правитель Ивелтине?",
        "Король Ивелтин",
        "Правитель Ивэлтин",
        "Правитель Ивелтинского Королевства",
    ],
)
def test_ivelthin_variants_share_ruler_expansion(
    lexicon: SynonymLexicon,
    query: str,
) -> None:
    expansion = lexicon.expand(query)
    normalized = lexicon.normalize(expansion.canonical_query)
    assert "ивелтин" in normalized
    assert "правитель" in expansion.expanded_query
    assert "король" in expansion.expanded_query


def test_dirty_discord_location_chunk_is_rejected() -> None:
    dirty = _hit(
        hit_id="dirty",
        title="Республика Ивелтин - Горы Канар-Дзегун",
        source_id="discord:location-sync",
        content=(
            "Официальные сведения об объекте «Локация»: "
            "Ты сама мне на член прыгнула. Вам снято -5 морали из-за дебаффа."
        ),
        score=9.0,
    )
    assert contamination_reasons(dirty)
    assert filter_and_rank("Кто правитель Ивелтине", [dirty], corpus=Corpus.LORE, limit=5) == []


def test_root_wiki_page_beats_location_noise() -> None:
    clean = _hit(
        hit_id="clean",
        title="Королевство Ивелтин",
        content=(
            "Нынешний правитель государства — король Артур II Вир Ивелтин. "
            "Он возглавляет Королевство Ивелтин."
        ),
        score=1.2,
    )
    location = _hit(
        hit_id="location",
        title="Республика Ивелтин - Высота Джаки",
        content="Высота Джаки расположена в горном регионе и покрыта лугами.",
        score=8.0,
    )
    ranked = filter_and_rank(
        "Кто правитель Ивелтине",
        [location, clean],
        corpus=Corpus.LORE,
        limit=5,
    )
    assert ranked
    assert ranked[0].id == "clean"
    answer = structured_lore_answer("Кто правитель Ивелтине", ranked)
    assert answer is not None
    assert "Артур II" in answer
    assert "Официальные сведения об объекте" not in answer


def test_public_guard_detects_raw_retrieval_dump() -> None:
    reasons = public_contamination_reasons(
        "Официальные сведения об объекте «Локация»: чужой сырой текст"
    )
    assert "raw_retrieval_dump" in reasons
