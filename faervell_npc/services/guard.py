from __future__ import annotations

import re
from dataclasses import dataclass, field

from faervell_npc.schemas import ActorPacket


@dataclass(slots=True)
class GuardResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


class OutputGuard:
    OOC_PATTERNS = {
        "gm_reference": re.compile(r"(?<![\wа-яё])(?:гм|gm)(?![\wа-яё])", re.IGNORECASE),
        "admin_reference": re.compile(r"\b(?:администратор|модератор)\w*", re.IGNORECASE),
        "ticket_reference": re.compile(r"\b(?:тикет|заявк|одобрени)\w*", re.IGNORECASE),
    }

    MODERNISMS = {
        "окей",
        "лол",
        "кринж",
        "вайб",
        "интернет",
        "гугл",
        "дискорд",
        "чатгпт",
        "нейросеть",
    }

    def validate(self, text: str, packet: ActorPacket) -> GuardResult:
        violations: list[str] = []
        lowered = text.casefold()
        words = re.findall(r"[\wа-яё-]+", text, flags=re.IGNORECASE)

        if len(words) > packet.max_length_words:
            violations.append("response_too_long")

        for forbidden in packet.facts_forbidden:
            if forbidden and forbidden.casefold() in lowered:
                violations.append(f"forbidden_fact:{forbidden[:60]}")

        for mention in packet.required_mentions:
            if mention and mention.casefold() not in lowered:
                violations.append(f"missing_required_mention:{mention[:60]}")

        modern = sorted(term for term in self.MODERNISMS if term in lowered)
        if modern:
            violations.append("modernisms:" + ",".join(modern))

        allowed_numbers = self._numbers(" ".join(packet.facts_allowed + packet.required_mentions))
        output_numbers = self._numbers(text)
        unapproved_numbers = output_numbers - allowed_numbers
        if unapproved_numbers:
            violations.append("unapproved_numbers:" + ",".join(sorted(unapproved_numbers)))

        if "как ии" in lowered or "языковая модель" in lowered:
            violations.append("out_of_character_ai_reference")

        for name, pattern in self.OOC_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"out_of_character_moderation:{name}")

        return GuardResult(passed=not violations, violations=violations)

    @staticmethod
    def _numbers(text: str) -> set[str]:
        return set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)", text))
