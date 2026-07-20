from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from faervell_npc.schemas import Corpus, KnowledgeHit

_WORD_RE = re.compile(r"(?iu)[а-яёa-z0-9-]{2,}")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

_FUNCTION_WORDS = {
    "а",
    "без",
    "бы",
    "в",
    "вам",
    "вас",
    "во",
    "вот",
    "где",
    "да",
    "для",
    "до",
    "его",
    "ее",
    "если",
    "есть",
    "и",
    "из",
    "или",
    "как",
    "когда",
    "кто",
    "ли",
    "мне",
    "на",
    "над",
    "не",
    "но",
    "о",
    "об",
    "он",
    "она",
    "они",
    "от",
    "по",
    "под",
    "про",
    "с",
    "со",
    "что",
    "это",
}

_INTENT_WORDS = {
    "где",
    "глава",
    "государство",
    "государь",
    "дата",
    "империя",
    "календарь",
    "княжество",
    "королевство",
    "король",
    "местонахождение",
    "монарх",
    "нынешний",
    "правитель",
    "правит",
    "расположение",
    "регион",
    "республика",
    "сезон",
    "управляет",
}

_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "его",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
    "иях",
    "ью",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ой",
    "ий",
    "ый",
    "ая",
    "яя",
    "ое",
    "ее",
    "ую",
    "юю",
    "ам",
    "ям",
    "ом",
    "ем",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
)

_HARD_CONTAMINATION = {
    "sexual_discord_fragment": re.compile(
        r"(?iu)(?:ты\s+сама\s+мне\s+на\s+член|я\s+тебя\s+не\s+насиловал|"
        r"потрахал|потрахались|секс(?:уальн|ом|а)|изнасиловал)"
    ),
    "discord_debuff": re.compile(
        r"(?iu)(?:вам\s+снято\s*-?\d+\s+морал|дебафф|любитель\s+пушистых)"
    ),
    "raw_chat_export": re.compile(
        r"(?im)^\s*(?:\[?[A-ZА-ЯЁ]{2,}\]?\s+)?[^\n]{0,80}\s+[—-]\s+\d{1,2}:\d{2}\s*$"
    ),
}

_RULER_RE = re.compile(
    r"(?iu)\b(?:корол|правител|монарх|государ|властител|глава\s+государств|правит|управля)\w*"
)
_LOCATION_RE = re.compile(r"(?iu)\b(?:где|располож|местонахожд|континент|регион)\w*")
_DATE_RE = re.compile(r"(?iu)\b(?:дата|число|день|год|сезон|календар)\w*")

_ENTITY_TYPES = {
    "герцогств",
    "государств",
    "импери",
    "княжеств",
    "королевств",
    "республик",
}


@dataclass(frozen=True, slots=True)
class QueryProfile:
    normalized: str
    intent: str
    entity_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: KnowledgeHit
    score: float
    title_score: float
    reasons: tuple[str, ...]


def normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", html.unescape(value).casefold().replace("ё", "е"))
    without_accents = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    return " ".join(re.sub(r"[^а-яa-z0-9 -]+", " ", without_accents).split())


def light_stem(value: str) -> str:
    word = normalize(value)
    if " " in word or len(word) < 5:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def consonant_key(value: str) -> str:
    word = normalize(value)
    return re.sub(r"[аеёиоуыэюяьъ\W_]", "", word)


def query_profile(query: str) -> QueryProfile:
    normalized = normalize(query)
    if _RULER_RE.search(normalized):
        intent = "ruler"
    elif _LOCATION_RE.search(normalized):
        intent = "location"
    elif _DATE_RE.search(normalized):
        intent = "date"
    else:
        intent = "generic"

    entities: list[str] = []
    for token in _WORD_RE.findall(normalized):
        stem = light_stem(token)
        if (
            len(stem) >= 4
            and stem not in _FUNCTION_WORDS
            and stem not in _INTENT_WORDS
            and stem not in _ENTITY_TYPES
            and stem not in entities
        ):
            entities.append(stem)
    return QueryProfile(normalized=normalized, intent=intent, entity_terms=tuple(entities[:6]))


def contamination_reasons(hit: KnowledgeHit) -> tuple[str, ...]:
    content = hit.content or ""
    normalized_content = normalize(content)
    reasons = [name for name, pattern in _HARD_CONTAMINATION.items() if pattern.search(content)]

    if content.casefold().count("официальные сведения об объекте") > 1:
        reasons.append("nested_retrieval_dump")
    if len(re.findall(r"(?iu)\bлокация\s*:", content)) > 1:
        reasons.append("multiple_location_chunks")
    if len(re.findall(r"(?iu)республика\s+ивелтин\s*[-—]", content)) > 1:
        reasons.append("concatenated_ivelthin_locations")
    if len(content) > 12_000 and content.count("\n") > 20:
        reasons.append("oversized_mixed_chunk")

    source_id = (hit.source_id or "").casefold()
    metadata = {str(key).casefold(): value for key, value in (hit.metadata or {}).items()}
    source_kind = str(metadata.get("source_kind") or metadata.get("kind") or "").casefold()
    trusted = bool(metadata.get("trusted") or metadata.get("structured"))
    looks_discord = "discord" in source_id or "discord" in source_kind
    dialogue_marks = content.count("—") + content.count(">")
    if looks_discord and not trusted and dialogue_marks >= 3:
        reasons.append("unstructured_discord_dialogue")

    if "морали" in normalized_content and "локаци" in normalize(hit.title):
        reasons.append("mechanics_inside_lore_location")

    return tuple(dict.fromkeys(reasons))


def _token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.92
    left_key = consonant_key(left)
    right_key = consonant_key(right)
    if len(left_key) >= 3 and left_key == right_key:
        return 0.9
    return SequenceMatcher(a=left, b=right).ratio()


def title_entity_score(profile: QueryProfile, title: str) -> float:
    if not profile.entity_terms:
        return 0.5
    title_terms = [light_stem(token) for token in _WORD_RE.findall(normalize(title))]
    if not title_terms:
        return 0.0
    matches: list[float] = []
    for entity in profile.entity_terms:
        matches.append(max((_token_similarity(entity, term) for term in title_terms), default=0.0))
    return sum(matches) / len(matches)


def _root_title_bonus(profile: QueryProfile, title: str) -> float:
    if not profile.entity_terms:
        return 0.0
    title_terms = [light_stem(token) for token in _WORD_RE.findall(normalize(title))]
    extras = [
        term
        for term in title_terms
        if term not in profile.entity_terms
        and term not in _ENTITY_TYPES
        and max((_token_similarity(term, entity) for entity in profile.entity_terms), default=0.0)
        < 0.85
    ]
    if len(extras) <= 1:
        return 0.55
    if len(extras) <= 3:
        return 0.1
    return -0.4


def _intent_score(profile: QueryProfile, hit: KnowledgeHit) -> float:
    haystack = normalize(f"{hit.title} {hit.metadata.get('section', '')} {hit.content[:4000]}")
    if profile.intent == "ruler":
        return 0.75 if _RULER_RE.search(haystack) else -0.55
    if profile.intent == "location":
        return 0.55 if _LOCATION_RE.search(haystack) else -0.2
    if profile.intent == "date":
        return 0.55 if _DATE_RE.search(haystack) else -0.2
    return 0.0


def _source_bonus(hit: KnowledgeHit) -> float:
    source_id = (hit.source_id or "").casefold()
    url = (hit.url or "").casefold()
    if source_id.startswith("faervell_wiki_root:") or "faervellrp.fandom.com" in url:
        return 0.5
    if "wiki" in source_id or "fandom" in source_id:
        return 0.3
    if "discord" in source_id:
        return -0.25
    return 0.0


def rank_hit(query: str, hit: KnowledgeHit) -> RankedHit:
    profile = query_profile(query)
    title_score = title_entity_score(profile, hit.title)
    base = min(1.0, max(0.0, float(hit.score or 0.0)) / 2.0)
    score = base + 1.6 * title_score + _intent_score(profile, hit) + _source_bonus(hit)
    if profile.intent == "ruler":
        score += _root_title_bonus(profile, hit.title)
    reasons: list[str] = [f"base={base:.3f}", f"title={title_score:.3f}"]
    return RankedHit(hit=hit, score=score, title_score=title_score, reasons=tuple(reasons))


def filter_and_rank(
    query: str,
    hits: Iterable[KnowledgeHit],
    *,
    limit: int,
    corpus: Corpus | None = None,
) -> list[KnowledgeHit]:
    profile = query_profile(query)
    ranked: list[RankedHit] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.id in seen:
            continue
        seen.add(hit.id)
        if corpus is not None and hit.corpus != corpus:
            continue
        if contamination_reasons(hit):
            continue
        item = rank_hit(query, hit)
        if (
            profile.intent != "generic"
            and profile.entity_terms
            and item.title_score < 0.52
        ):
            continue
        if profile.intent == "ruler":
            ruler_haystack = normalize(
                f"{hit.title} {hit.metadata.get('section', '')} {hit.content[:4000]}"
            )
            if not _RULER_RE.search(ruler_haystack) or item.score < 1.15:
                continue
        ranked.append(item)

    ranked.sort(key=lambda item: (item.score, float(item.hit.score or 0.0)), reverse=True)
    result: list[KnowledgeHit] = []
    for item in ranked[:limit]:
        result.append(item.hit.model_copy(update={"score": item.score}))
    return result


def has_confident_hit(query: str, hits: list[KnowledgeHit]) -> bool:
    if not hits:
        return False
    profile = query_profile(query)
    top = rank_hit(query, hits[0])
    if profile.entity_terms:
        return top.title_score >= 0.78 and top.score >= 1.45
    return top.score >= 0.85


def sanitize_source_content(content: str, *, max_chars: int = 1200) -> str:
    value = html.unescape(content or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"(?m)^\s*>+\s?", "", value)
    value = re.sub(r"(?m)^\s*[-*#]{1,4}\s+", "", value)
    value = re.sub(r"<@!?\d+>|<#\d+>|<@&\d+>", "", value)
    value = re.sub(r"(?iu)официальные сведения об объекте\s*«[^»]+»\s*:\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > max_chars:
        value = value[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return value


def extract_relevant_excerpt(query: str, hit: KnowledgeHit, *, max_chars: int = 900) -> str:
    clean = sanitize_source_content(hit.content, max_chars=6000)
    if not clean:
        return ""
    profile = query_profile(query)
    candidates = [part.strip() for part in _SENTENCE_RE.split(clean) if part.strip()]
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(candidates):
        pseudo = hit.model_copy(update={"content": sentence})
        if contamination_reasons(pseudo):
            continue
        normalized_sentence = normalize(sentence)
        score = 0.0
        for entity in profile.entity_terms:
            if entity in normalized_sentence:
                score += 1.0
        if profile.intent == "ruler" and _RULER_RE.search(normalized_sentence):
            score += 2.0
        elif profile.intent == "location" and _LOCATION_RE.search(normalized_sentence):
            score += 1.5
        elif profile.intent == "date" and _DATE_RE.search(normalized_sentence):
            score += 1.5
        if index < 3:
            score += 0.2
        scored.append((score, index, sentence))

    if not scored:
        return sanitize_source_content(clean, max_chars=max_chars)
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    selected = sorted(scored[:3], key=lambda item: item[1])
    excerpt = " ".join(item[2] for item in selected)
    return sanitize_source_content(excerpt, max_chars=max_chars)


def safe_source_fact(query: str, hit: KnowledgeHit) -> str:
    excerpt = extract_relevant_excerpt(query, hit)
    return f"{hit.title}: {excerpt}" if excerpt else hit.title


def structured_lore_answer(query: str, hits: list[KnowledgeHit]) -> str | None:
    profile = query_profile(query)
    if profile.intent != "ruler" or not hits:
        return None
    top = hits[0]
    if title_entity_score(profile, top.title) < 0.78:
        return None
    excerpt = extract_relevant_excerpt(query, top, max_chars=520)
    if not excerpt or not _RULER_RE.search(normalize(excerpt)):
        return None
    return (
        "Странник отвечает после короткой сверки с записями.\n\n"
        f"— {excerpt}"
    )


def public_contamination_reasons(text: str) -> tuple[str, ...]:
    reasons = [name for name, pattern in _HARD_CONTAMINATION.items() if pattern.search(text)]
    lowered = text.casefold()
    if "официальные сведения об объекте" in lowered:
        reasons.append("raw_retrieval_dump")
    if lowered.count("официальные сведения об объекте") > 1:
        reasons.append("multiple_raw_retrieval_dumps")
    return tuple(dict.fromkeys(reasons))
