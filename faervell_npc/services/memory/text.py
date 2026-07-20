from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def normalize_text(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = _PUNCT_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def lexical_similarity(left: str, right: str) -> float:
    a = set(normalize_text(left).split())
    b = set(normalize_text(right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def tokens(value: str) -> set[str]:
    return set(normalize_text(value).split())


def join_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def estimate_tokens(value: str) -> int:
    # Deterministic and conservative enough for prompt budgeting without an API call.
    return max(1, (len(value) + 3) // 4)


def compact_sentences(value: str, budget_tokens: int) -> str:
    if estimate_tokens(value) <= budget_tokens:
        return value
    parts = re.split(r"(?<=[.!?])\s+|\n+", value.strip())
    result: list[str] = []
    used = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        cost = estimate_tokens(part)
        if used + cost > budget_tokens:
            break
        result.append(part)
        used += cost
    return " ".join(result) or value.split()[0][: max(1, budget_tokens * 4)]
