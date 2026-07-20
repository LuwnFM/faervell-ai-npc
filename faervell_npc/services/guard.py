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
        "source_footer": re.compile(
            r"(?im)^\s*(?:[-#]\s*)?(?:источник|источники)\s*:|\bпо источнику\b"
        ),
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

        attribution_markers = ("по словам", "мне рассказывали", "я слышал", "говорил", "утверждал", "слух")
        for testimony in packet.recalled_testimonies:
            status = str(testimony.get("trust_status", ""))
            mode = str(testimony.get("attribution_mode", ""))
            speaker = str(testimony.get("speaker_name") or "").strip()
            content = str(testimony.get("content") or "").strip()
            if mode in {"ANONYMOUS", "PRIVATE"} and speaker and speaker.casefold() in lowered:
                violations.append("forbidden_testimony_source")
            if content and content.casefold() in lowered and status not in {"CONFIRMED", "OBSERVED"}:
                if not any(marker in lowered for marker in attribution_markers):
                    violations.append("testimony_presented_as_fact")

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

        # The RP body must be fully Russian. The model slug is appended later by Discord
        # and is therefore not part of this validation.
        if re.search(r"[A-Za-z]", text):
            violations.append("latin_characters_in_rp_body")

        if self._looks_incomplete(text):
            violations.append("incomplete_or_truncated_response")

        for name, pattern in self.OOC_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"out_of_character_moderation:{name}")

        return GuardResult(passed=not violations, violations=violations)

    @staticmethod
    def _looks_incomplete(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        if stripped[-1] in {",", ":", ";", "—", "-", "(", "[", "{"}:
            return True
        # A normal RP answer must close its final sentence. This catches provider outputs
        # such as «Странник сжимает зубы,» even when finish_reason was incorrectly "stop".
        if stripped[-1] not in ".!?…»”\"')]}*":
            return True
        if stripped.count("*") % 2:
            return True
        if stripped.count("(") != stripped.count(")"):
            return True
        if stripped.count("[") != stripped.count("]"):
            return True
        return False

    @staticmethod
    def _numbers(text: str) -> set[str]:
        return set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)", text))
