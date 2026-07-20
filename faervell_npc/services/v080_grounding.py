from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import SourceRevision
from faervell_npc.schemas import ActorPacket, ResponseType

# v0.8 keeps retrieval in PostgreSQL/pgvector and fixes the layers around it:
# decomposition, partial answers, packet repair, gap normalization and output grounding.

FACET_PATTERNS: dict[str, tuple[str, ...]] = {
    "ruler": (r"\bкорол\w*", r"\bправител\w*", r"\bмонарх\w*", r"\bглава\w*"),
    "location": (
        r"\bгде\b",
        r"\bрасполож\w*",
        r"\bместонахожд\w*",
    ),
    "route": (
        r"\bкак\s+(?:туда|до\s+\S+)\s+(?:пройти|попасть|добраться)",
        r"\bкак\s+добраться",
        r"\bмаршрут\w*",
        r"\bдорог(?:а|у|ой|и|е)\b",
        r"\bпуть\b",
    ),
    "currency": (r"\bвалют\w*", r"\bмонет\w*", r"\bденьг\w*", r"\bчем\s+плат\w*"),
    "religion": (r"\bрелиги\w*", r"\bвероисповед\w*", r"\bвер\w*", r"\bцерк\w*"),
    "countries": (r"\b(?:какие|список)\s+(?:страны|государства|королевства)", r"\bстраны\s+там\b"),
    "wars": (r"\bбитв\w*", r"\bвойн\w*", r"\bсражени\w*", r"\bосад\w*", r"\bконфликт\w*"),
    "year": (r"\bкакой\s+(?:сейчас\s+)?год\b", r"\bтекущ\w*\s+год\b", r"\bлетоисчислен\w*"),
    "date": (r"\bдат\w*", r"\bкакое\s+(?:сейчас\s+)?число\b", r"\bкакой\s+(?:сейчас\s+)?день\b"),
    "time": (r"\bкоторый\s+час\b", r"\bточн\w*\s+врем\w*", r"\bсколько\s+времени\b", r"\bвремя\b"),
}

FACET_LABELS: dict[str, str] = {
    "ruler": "нынешний правитель",
    "location": "местоположение",
    "route": "точный путь или маршрут",
    "currency": "валюта",
    "religion": "религия",
    "countries": "список государств",
    "wars": "подтверждённые войны и битвы",
    "year": "текущий год",
    "date": "текущая дата",
    "time": "точное время",
}

FACET_COVERAGE: dict[str, tuple[str, ...]] = {
    "ruler": ("правитель", "король", "королева", "монарх", "герцог", "совет республики", "регент"),
    "location": ("располож", "находится", "континент", "регион", "столица"),
    "route": ("маршрут", "дорога", "путь", "добраться", "пройти через", "перевал", "тракт", "ворота"),
    "currency": ("валюта", "монета", "деньги", "расчёт", "платёж", "денар", "крона", "талер"),
    "religion": ("религ", "вероисповед", "церковь", "культ", "вера"),
    "countries": ("королевство", "республика", "империя", "герцогство", "княжество", "деспотия", "конфедерация"),
    "wars": ("война", "битва", "сражение", "осада", "восстание", "мятеж", "боевые столкновения", "операция"),
    "year": (" год", "года", "летоисчис"),
    "date": ("текущая дата", "на момент", "сегодня", "число", "весна ", "лето ", "осень ", "зима "),
    "time": ("точное время", "час:", "время суток", "утро", "полдень", "вечер", "ночь"),
}

NOISE_GAP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?iu)^\s*(?:ты\s+)?про\s+что(?:\s+щас|\s+сейчас)?\??\s*$"),
    re.compile(r"(?iu)^\s*чего\s+что\??\s*$"),
    re.compile(r"(?iu)^\s*что\s+ты\s+(?:тут|здесь)\s+делаешь\??\s*$"),
    re.compile(r"(?iu)^\s*где\s+мы\s+находимся\??\s*$"),
    re.compile(r"(?iu)^\s*(?:а\s+)?кто\s+ты\??\s*$"),
    re.compile(r"(?iu)^\s*(?:привет|здравствуй|алло|эй)[!.?\s]*$"),
)

SERVICE_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?iu)ивентовая\s+политика"),
    re.compile(r"(?iu)правила\s+(?:сервера|ивентов)"),
    re.compile(r"(?iu)архитектура\s+ии"),
    re.compile(r"(?iu)служебн"),
)

PENDING_PREMATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?iu)можешь\s+отправляться"),
    re.compile(r"(?iu)передать\s+(?:тебе\s+)?(?:пакет|предмет).{0,30}(?:прямо\s+)?сейчас"),
    re.compile(r"(?iu)условия\s+ясны"),
    re.compile(r"(?iu)дело\s+называется"),
    re.compile(r"(?iu)плата\s+(?:после|за)\s+выполн"),
)

FOREIGN_SCRIPT_RE = re.compile(
    "["
    "\u3040-\u30ff"  # Japanese
    "\u3400-\u4dbf\u4e00-\u9fff"  # CJK
    "\uac00-\ud7af\u1100-\u11ff"  # Korean
    "\u0e00-\u0e7f"  # Thai
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "]"
)

FACT_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?iu)\b(?:королевств|республик|импери|герцогств|княжеств|деспоти|конфедераци|континент|город|столиц|остров|перевал|орден|гильди)\w*\s+"
        r"(?:[«\"“„]?)([А-ЯЁ][А-ЯЁа-яё'’\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё'’\-]+){0,3})"
    ),
    re.compile(
        r"(?iu)\b(?:корол|королев|герцог|герцогин|регент|лорд-коммандор|адмирал|генерал)\w*\s+"
        r"([А-ЯЁ][А-ЯЁа-яё'’\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё'’\-]+){0,4})"
    ),
    re.compile(
        r"(?iu)\b(?:войн|битв|сражени|осад|операци|смут|кризис)\w*\s+"
        r"[«\"“„]?([А-ЯЁ][А-ЯЁа-яё'’\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё'’\-]+){0,4})"
    ),
    re.compile(
        r"[«\"“„]([А-ЯЁ][А-ЯЁа-яё'’\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё'’\-]+){0,4})[»\"”]"
    ),
)

ALLOWED_STAGE_NAMES = {
    "странник",
    "хлад",
    "око развилки",
    "ключ тысячи порогов",
    "нулевой якорь",
    "хранилище потерянного",
}


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        key = _fold(clean)
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def requested_facets(query: str) -> list[str]:
    folded = _fold(query)
    result: list[str] = []
    for facet, patterns in FACET_PATTERNS.items():
        if any(re.search(pattern, folded) for pattern in patterns):
            result.append(facet)
    return result


def covered_facets(facts: Sequence[str]) -> set[str]:
    blob = _fold("\n".join(facts))
    result: set[str] = set()
    for facet, hints in FACET_COVERAGE.items():
        if any(_fold(hint) in blob for hint in hints):
            result.add(facet)
    # A concrete world date such as 12.06.1253 covers year and date.
    if re.search(r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:1[0-9]{3}|[1-9]\d{2,3})\b", blob):
        result.update({"year", "date"})
    elif re.search(r"\b1[0-9]{3}\s+год", blob):
        result.add("year")
    return result


def missing_facets(query: str, facts: Sequence[str]) -> list[str]:
    covered = covered_facets(facts)
    return [facet for facet in requested_facets(query) if facet not in covered]


def missing_fact_sentence(missing: Sequence[str]) -> str:
    labels = [FACET_LABELS[item] for item in missing if item in FACET_LABELS]
    if not labels:
        return ""
    if len(labels) == 1:
        return f"В доступных подтверждённых сведениях не указано: {labels[0]}."
    joined = ", ".join(labels[:-1]) + " и " + labels[-1]
    return f"В доступных подтверждённых сведениях не указаны: {joined}."


def is_noise_gap(question: str) -> bool:
    clean = re.sub(r"\s+", " ", question).strip()
    if len(clean) < 8:
        return True
    return any(pattern.match(clean) for pattern in NOISE_GAP_PATTERNS)


def normalize_gap_question(question: str, missing: Sequence[str] | None = None) -> str:
    clean = re.sub(r"\s+", " ", question).strip(" \t\n.!?")
    clean = re.sub(r"(?iu)^(?:привет|здравствуй|добрый\s+(?:день|вечер))[,!.\s]+", "", clean)
    if missing:
        labels = [FACET_LABELS[item] for item in missing if item in FACET_LABELS]
        entities = _extract_query_entities(clean)
        subject = ", ".join(entities[:3]) or "этой теме"
        return f"Уточнить: {', '.join(labels)} — {subject}."[:700]
    # Normalize the common Iveltin duplicates without hard-coding an answer.
    entities = _extract_query_entities(clean)
    facets = requested_facets(clean)
    if entities and facets:
        return normalize_gap_question(clean, facets)
    return clean[:700] + ("?" if clean and not clean.endswith("?") else "")


def normalize_gap_key(question: str) -> str:
    clean = _fold(question)
    clean = re.sub(r"[^a-zа-я0-9]+", " ", clean)
    stop = {
        "мне", "нужна", "нужно", "информация", "скажи", "расскажи", "пожалуйста",
        "сейчас", "щас", "там", "про", "об", "о", "и", "а", "кто", "где", "какой",
        "какая", "какие", "мы", "говорили", "остался", "неназванным", "как", "его", "зовут",
    }
    words = [word for word in clean.split() if word not in stop and len(word) > 2]
    return " ".join(sorted(set(words)))[:500]


def scrub_model_error(value: str, limit: int = 600) -> str:
    text = str(value or "")
    text = re.sub(r"(?iu)user_[A-Za-z0-9_-]+", "user_[скрыто]", text)
    text = re.sub(r"(?iu)(authorization|api[_ -]?key|token)[\"' :=]+[^\s,}\]]+", r"\1=[скрыто]", text)
    text = re.sub(r"(?s);?\s*response=\{.*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def safe_evidence(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        title = str(item.get("title") or "")
        if any(pattern.search(title) for pattern in SERVICE_TITLE_PATTERNS):
            continue
        identity = str(item.get("knowledge_id") or item.get("id") or item.get("source_id") or title)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                key: item.get(key)
                for key in ("id", "knowledge_id", "source_id", "title", "url", "revision", "score")
                if item.get(key) is not None
            }
        )
    return result[:8]


def extract_tool_evidence(tool_results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[Mapping[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("title") and any(
                value.get(key) is not None
                for key in ("id", "knowledge_id", "source_id", "url", "revision")
            ):
                items.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(tool_results)
    return safe_evidence(items)


def extract_disclosable_tool_facts(tool_results: Sequence[Mapping[str, Any]]) -> list[str]:
    facts: list[str] = []

    def walk(value: Any, parent: Mapping[str, Any] | None = None) -> None:
        if isinstance(value, Mapping):
            allowed = value.get("may_disclose")
            if allowed is not False:
                title = str(value.get("title") or "").strip()
                content = value.get("content") or value.get("free_summary")
                if isinstance(content, str) and content.strip():
                    clean = content.strip()
                    if title and _fold(title) not in _fold(clean):
                        clean = f"{title}: {clean}"
                    facts.append(clean)
                for key, label in (
                    ("game_date", "Текущая дата мира"),
                    ("world_date", "Текущая дата мира"),
                    ("current_date", "Текущая дата мира"),
                    ("game_time", "Текущее время мира"),
                    ("world_time", "Текущее время мира"),
                ):
                    item = value.get(key)
                    if item not in (None, "", "UNKNOWN", "NOT_CONNECTED"):
                        facts.append(f"{label}: {item}.")
            for child in value.values():
                walk(child, value)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, parent)

    walk(tool_results)
    return _unique(facts)


def repair_actor_packet(
    packet: ActorPacket,
    *,
    player_message: str,
    tool_results: Sequence[Mapping[str, Any]],
    context: Any,
) -> ActorPacket:
    packet.scene_id = context.scene_id
    packet.player_name = context.player_name
    packet.profession_mask_id = context.profession_mask_id
    packet.location_name = context.location_name
    packet.action_result = dict(packet.action_result or {})
    packet.action_result["player_said"] = player_message
    if packet.response_type in {ResponseType.LORE_ANSWER, ResponseType.MECHANICS_ANSWER}:
        packet.action_result["canon_overrides_dialogue_history"] = True

    tool_facts = extract_disclosable_tool_facts(tool_results)
    existing = list(packet.facts_allowed or [])
    if not existing and tool_facts:
        existing = tool_facts[:8]
    else:
        # Preserve the model's grounded compact wording and only add tool facts needed
        # to avoid an empty/partial packet.
        missing = missing_facets(player_message, existing)
        for fact in tool_facts:
            if not missing:
                break
            trial = _unique([*existing, fact])
            if len(missing_facets(player_message, trial)) < len(missing):
                existing = trial
                missing = missing_facets(player_message, existing)

    missing = missing_facets(player_message, existing)
    unknown = missing_fact_sentence(missing)
    if unknown:
        existing = _unique([*existing, unknown])
        packet.action_result["missing_facets"] = missing
    packet.facts_allowed = existing[:10]

    if packet.facts_allowed and packet.response_type == ResponseType.SAFE_UNKNOWN:
        packet.response_type = ResponseType.LORE_ANSWER
    if packet.response_type == ResponseType.LORE_ANSWER and not packet.facts_allowed:
        packet.response_type = ResponseType.SAFE_UNKNOWN

    status = str(packet.action_result.get("status") or "").upper()
    if status in {"PENDING", "PENDING_GM", "PENDING_REVIEW"}:
        packet.quest_summary = None
        packet.facts_allowed = [
            "Работа найдётся, но сперва нужно уточнить конкретную цель, условия и плату.",
            "Пока поручение не подтверждено, отправляться или забирать предметы не нужно.",
        ]
        packet.required_mentions = ["нужно уточнить"]
        packet.max_length_words = min(packet.max_length_words, 120)
    return packet


async def latest_world_clock_fact(session: AsyncSession) -> str | None:
    rows = (
        await session.execute(
            select(SourceRevision).where(SourceRevision.source_id.like("discord_world_news:%"))
        )
    ).scalars().all()
    candidates: list[tuple[datetime, str]] = []
    for row in rows:
        metadata = dict(row.metadata_json or {})
        label = str(metadata.get("world_date_label") or "").strip()
        timestamp = str(metadata.get("discord_created_at") or "")
        if not label:
            continue
        try:
            created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            created = row.fetched_at
        candidates.append((created, label))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return f"Последняя подтверждённая дата в новостях мира: {candidates[0][1]}."


def foreign_script_violations(text: str) -> list[str]:
    violations: list[str] = []
    if FOREIGN_SCRIPT_RE.search(text):
        violations.append("foreign_script_in_rp_body")
    stripped = text.lstrip()
    if stripped.startswith("{") or re.search(r'(?iu)"(?:response_type|facts_allowed|tool_results|actor_packet)"\s*:', text):
        violations.append("structured_payload_in_rp_body")
    if re.search(r"(?iu)\b(?:reasoning|completion)\s*\n?\s*\d*/\d*", text):
        violations.append("reasoning_label_in_rp_body")
    return violations


def ungrounded_lore_violations(text: str, packet: ActorPacket) -> list[str]:
    if packet.response_type not in {ResponseType.LORE_ANSWER, ResponseType.MECHANICS_ANSWER}:
        return []
    allowed = _fold(
        "\n".join(
            [
                *packet.facts_allowed,
                *packet.required_mentions,
                packet.location_name or "",
                packet.player_name or "",
                *ALLOWED_STAGE_NAMES,
            ]
        )
    )
    unknown: list[str] = []
    for pattern in FACT_ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            entity = re.sub(r"[»\"”.,:;!?]+$", "", match.group(1)).strip()
            folded = _fold(entity)
            if len(folded) < 4 or folded in allowed:
                continue
            # A generated phrase may contain a longer form than the source. Accept it only
            # when every significant word occurs in the allowed facts.
            words = [word for word in folded.split() if len(word) >= 4]
            if words and all(word in allowed for word in words):
                continue
            unknown.append(entity)
    return ["ungrounded_lore_entity:" + ",".join(_unique(unknown)[:5])] if unknown else []


_CLAIM_STOPWORDS = {
    "который", "которая", "которые", "этого", "этой", "такого", "такой", "здесь",
    "туда", "отсюда", "тогда", "теперь", "просто", "именно", "можно", "нужно",
    "будет", "были", "было", "есть", "этот", "через", "после", "перед", "между",
    "среди", "всего", "того", "если", "когда", "пока", "чтобы", "лишь", "только",
}
_CLAIM_EXEMPT_RE = re.compile(
    r"(?iu)^(?:если\b|по\s+этой\s+части|в\s+записях|в\s+архиве|у\s+меня|я\s+)"
    r".*(?:не\s+зна|нет\s+подтвержден|не\s+указан|не\s+стану|могу\s+сказать|могу\s+уточнить)"
)


def _claim_stems(value: str) -> set[str]:
    words = re.findall(r"(?iu)[а-яё]{4,}", _fold(value))
    return {
        (word[:5] if len(word) >= 7 else word)
        for word in words
        if word not in _CLAIM_STOPWORDS
    }


def ungrounded_lore_claim_violations(text: str, packet: ActorPacket) -> list[str]:
    """Reject unsupported declarative lore while leaving stagecraft and uncertainty free.

    The actor may paraphrase verified facts, but a factual dialogue sentence must retain
    enough lexical anchors from ``facts_allowed``. This catches generic inventions such as
    “магия запрещена повсеместно” or “стран десятка два” even when no new proper name appears.
    """
    if packet.response_type not in {ResponseType.LORE_ANSWER, ResponseType.MECHANICS_ANSWER}:
        return []
    allowed_stems = _claim_stems("\n".join([*packet.facts_allowed, *packet.required_mentions]))
    if not allowed_stems:
        return []
    unsupported: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(("—", "–", "-", "«", "“")):
            continue
        line = line.lstrip("—–- «“\t").rstrip("»” ")
        for sentence in re.split(r"(?<=[.!?…])\s+", line):
            clean = sentence.strip()
            if not clean or clean.endswith("?") or _CLAIM_EXEMPT_RE.search(clean):
                continue
            if re.search(r"(?iu)\b(?:не\s+знаю|нет\s+подтверждения|не\s+указано|не\s+указаны|не\s+могу\s+подтвердить)\b", clean):
                continue
            stems = _claim_stems(clean)
            if len(stems) < 3:
                continue
            overlap = len(stems & allowed_stems)
            required = max(2, (len(stems) + 3) // 4)
            if overlap < required:
                unsupported.append(clean[:180])
    return ["ungrounded_lore_claim:" + " | ".join(unsupported[:3])] if unsupported else []


def pending_quest_violations(text: str, packet: ActorPacket) -> list[str]:
    status = str((packet.action_result or {}).get("status") or "").upper()
    if status not in {"PENDING", "PENDING_GM", "PENDING_REVIEW"}:
        return []
    return ["pending_quest_presented_as_active"] if any(p.search(text) for p in PENDING_PREMATURE_PATTERNS) else []


def sanitize_candidate_titles(values: Sequence[str]) -> list[str]:
    return _unique(
        value
        for value in values
        if value and not any(pattern.search(value) for pattern in SERVICE_TITLE_PATTERNS)
    )[:8]


def _extract_query_entities(query: str) -> list[str]:
    generic = {
        "Привет", "Мне", "Где", "Кто", "Как", "Какая", "Какой", "Какие", "А", "Что",
        "Королевство", "Республика", "Империя", "Страна", "Континент", "Король",
    }
    result = []
    for match in re.finditer(r"(?u)\b[А-ЯЁ][А-ЯЁа-яё'’\-]{2,}(?:\s+[А-ЯЁ][А-ЯЁа-яё'’\-]{2,}){0,2}", query):
        item = match.group(0).strip()
        if item.split()[0] not in generic:
            result.append(item)
    if not result:
        # Player messages are often all lower-case. Pick noun-like words after entity labels.
        fallback_match = re.search(
            r"(?iu)\b(?:королевств|республик|импери|континент|страна|город)\w*\s+([а-яё][а-яё'’\-]{3,})",
            query,
        )
        if fallback_match:
            result.append(fallback_match.group(1).capitalize())
    return _unique(result)
