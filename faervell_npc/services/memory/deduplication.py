from __future__ import annotations

from dataclasses import dataclass

from .text import lexical_similarity, normalize_text


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    duplicate: bool
    conflict: bool = False
    score: float = 0.0
    reason: str = ""


def compare_claims(
    left: str,
    right: str,
    *,
    left_trust: str,
    right_trust: str,
    left_dates: list[str] | None = None,
    right_dates: list[str] | None = None,
) -> DuplicateDecision:
    if normalize_text(left) == normalize_text(right):
        return DuplicateDecision(True, score=1.0, reason="normalized_claim_equal")
    score = lexical_similarity(left, right)
    if left_dates and right_dates and set(left_dates) != set(right_dates):
        return DuplicateDecision(False, conflict=True, score=score, reason="different_dates")
    if ("не " in normalize_text(left)) != ("не " in normalize_text(right)):
        return DuplicateDecision(False, conflict=True, score=score, reason="polarity_conflict")
    if left_trust != right_trust and {left_trust, right_trust} >= {"CONFIRMED", "RUMOR"}:
        return DuplicateDecision(False, conflict=True, score=score, reason="trust_scope_conflict")
    return DuplicateDecision(score >= 0.90, score=score, reason="lexical_similarity")
