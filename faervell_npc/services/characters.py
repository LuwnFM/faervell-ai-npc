from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import (
    CharacterBinding,
    CharacterProfile,
    SceneCharacterIdentity,
    SceneConfig,
)
from faervell_npc.schemas import IncomingMessage
from faervell_npc.services.embeddings import get_embedder

_FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    "canonical_name": (
        r"(?im)^\s*(?:1\.1\s*)?имя\s+персонажа\s*:\s*(.+?)\s*$",
        r"(?im)^\s*имя\s*:\s*(.+?)\s*$",
    ),
    "alias": (
        r"(?im)^\s*(?:1\.2\s*)?(?:прозвище|псевдоним|алиас)\s*:\s*(.+?)\s*$",
    ),
    "race": (
        r"(?im)^\s*(?:1\.3\s*)?раса(?:\s+персонажа)?(?:\s+и\s+(?:е[её]|ее)\s+подвид)?\s*:\s*(.+?)\s*$",
    ),
    "age_text": (r"(?im)^\s*(?:2\.1\s*)?возраст\s*:\s*(.+?)\s*$",),
    "sex": (r"(?im)^\s*(?:2\.2\s*)?пол\s*:\s*(.+?)\s*$",),
    "height_text": (r"(?im)^\s*(?:2\.3\s*)?рост\s*:\s*(.+?)\s*$",),
}

_APPEARANCE_HEADER_RE = re.compile(r"(?is)(?:^|\n)\s*внешность\s*:\s*(.+)$")
_IMAGE_MARKER_RE = re.compile(r"(?im)^\s*(?:изображение|арт|референс)\s*$")
_PRESENTATION_PATTERNS = (
    re.compile(
        r"(?iu)\bменя\s+зовут\s+([а-яёa-z][а-яёa-z'’\-]{1,40}(?:\s+[а-яёa-z][а-яёa-z'’\-]{1,40})?)"
    ),
    re.compile(
        r"(?iu)\bзови(?:те)?\s+меня\s+([а-яёa-z][а-яёa-z'’\-]{1,40}(?:\s+[а-яёa-z][а-яёa-z'’\-]{1,40})?)"
    ),
    re.compile(
        r"(?iu)\bпредставляюсь(?:\s+как)?\s+([а-яёa-z][а-яёa-z'’\-]{1,40}(?:\s+[а-яёa-z][а-яёa-z'’\-]{1,40})?)"
    ),
    re.compile(
        r"(?iu)\bмо[её]\s+имя\s+([а-яёa-z][а-яёa-z'’\-]{1,40}(?:\s+[а-яёa-z][а-яёa-z'’\-]{1,40})?)"
    ),
    re.compile(
        r"(?iu)(?:^|[.!?]\s*)я\s*[—–:-]\s*([а-яёa-z][а-яёa-z'’\-]{1,40}(?:\s+[а-яёa-z][а-яёa-z'’\-]{1,40})?)"
    ),
    re.compile(
        r"(?u)(?:^|[.!?]\s*)[Яя]\s+([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’\-]{1,40})(?=[,.!?\s]|$)"
    ),
 )
_PLAIN_NAME_RE = re.compile(
    r"(?u)^\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’\-]{1,40}(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’\-]{1,40}){0,3})\s*[.!?]?\s*$"
)
_APPEARANCE_STEMS = {
    "внешность",
    "выгляжу",
    "рост",
    "высокий",
    "низкий",
    "волосы",
    "глаза",
    "кожа",
    "лицо",
    "уши",
    "нос",
    "шрам",
    "ожог",
    "одет",
    "одета",
    "одежда",
    "доспех",
    "плащ",
    "гоблин",
    "человек",
    "эльф",
    "дворф",
    "орк",
    "зверолюд",
    "раса",
}


@dataclass(slots=True)
class ParsedCharacterSheet:
    canonical_name: str
    aliases: list[str]
    race: str | None
    age_text: str | None
    sex: str | None
    height_text: str | None
    height_cm: float | None
    appearance: str
    visible_profile: str
    searchable_identity: str


@dataclass(slots=True)
class Presentation:
    presented_name: str | None
    has_appearance: bool
    is_presentation: bool


@dataclass(slots=True)
class CharacterResolution:
    character_id: str
    display_name: str
    status: str
    confidence: float = 0.0
    requires_presentation: bool = False
    requires_name: bool = False


class CharacterSheetParser:
    @classmethod
    def parse(cls, text: str) -> ParsedCharacterSheet | None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        canonical_name = cls._field(normalized, "canonical_name")
        if not canonical_name:
            return None

        alias_text = cls._field(normalized, "alias")
        aliases = [canonical_name]
        if alias_text and cls._meaningful(alias_text):
            aliases.extend(cls._split_aliases(alias_text))
        aliases = cls._unique(aliases)

        race = cls._clean(cls._field(normalized, "race"))
        age_text = cls._clean(cls._field(normalized, "age_text"))
        sex = cls._clean(cls._field(normalized, "sex"))
        height_text = cls._clean(cls._field(normalized, "height_text"))
        height_cm = cls._parse_height_cm(height_text)
        appearance = cls._appearance(normalized)

        visible_parts = [f"Имя: {canonical_name}"]
        if len(aliases) > 1:
            visible_parts.append("Имена и прозвища: " + ", ".join(aliases))
        if race:
            visible_parts.append(f"Раса: {race}")
        if age_text:
            visible_parts.append(f"Возраст: {age_text}")
        if sex:
            visible_parts.append(f"Пол: {sex}")
        if height_text:
            visible_parts.append(f"Рост: {height_text}")
        if appearance:
            visible_parts.append("Внешность: " + appearance)

        visible_profile = "\n".join(visible_parts)
        searchable_identity = "\n".join(
            [canonical_name, " ".join(aliases), race or "", height_text or "", appearance]
        ).strip()
        return ParsedCharacterSheet(
            canonical_name=canonical_name,
            aliases=aliases,
            race=race,
            age_text=age_text,
            sex=sex,
            height_text=height_text,
            height_cm=height_cm,
            appearance=appearance,
            visible_profile=visible_profile,
            searchable_identity=searchable_identity,
        )

    @staticmethod
    def _field(text: str, key: str) -> str | None:
        for pattern in _FIELD_PATTERNS[key]:
            match = re.search(pattern, text)
            if match:
                return CharacterSheetParser._clean(match.group(1))
        return None

    @staticmethod
    def _appearance(text: str) -> str:
        match = _APPEARANCE_HEADER_RE.search(text)
        if not match:
            return ""
        appearance = match.group(1).strip()
        marker = _IMAGE_MARKER_RE.search(appearance)
        if marker:
            appearance = appearance[: marker.start()].strip()
        return re.sub(r"\n{3,}", "\n\n", appearance)[:8000]

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().strip("|▐▬—–- ").strip()
        cleaned = cleaned.rstrip(".;, ")
        return cleaned or None

    @staticmethod
    def _meaningful(value: str) -> bool:
        return value.strip().casefold() not in {"-", "—", "нет", "отсутствует", "не имеется"}

    @staticmethod
    def _split_aliases(value: str) -> list[str]:
        return [
            part.strip().strip('"«»').rstrip(".")
            for part in re.split(r"[,;/]|\s+или\s+", value, flags=re.IGNORECASE)
            if part.strip().strip('"«»').rstrip(".")
        ]

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = CharacterRegistryService.normalize_name(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @staticmethod
    def _parse_height_cm(value: str | None) -> float | None:
        if not value:
            return None
        lowered = value.casefold().replace(",", ".")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(см|сантиметр|м|метр)", lowered)
        if not match:
            return None
        number = float(match.group(1))
        unit = match.group(2)
        return number * 100 if unit in {"м", "метр"} else number


class CharacterRegistryService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()

    async def upsert_profile(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        owner_discord_user_id: str,
        source_channel_id: str,
        source_message_id: str,
        source_created_at: datetime | None,
        text: str,
        attachment_urls: list[str],
    ) -> CharacterProfile | None:
        parsed = CharacterSheetParser.parse(text)
        if parsed is None:
            return None

        profile_id = f"registry:{guild_id}:{source_message_id}"
        profile = await session.get(CharacterProfile, profile_id)
        values = {
            "guild_id": guild_id,
            "owner_discord_user_id": owner_discord_user_id,
            "source_channel_id": source_channel_id,
            "source_message_id": source_message_id,
            "canonical_name": parsed.canonical_name,
            "aliases": parsed.aliases,
            "race": parsed.race,
            "age_text": parsed.age_text,
            "sex": parsed.sex,
            "height_text": parsed.height_text,
            "height_cm": parsed.height_cm,
            "visible_profile": parsed.visible_profile,
            "full_sheet": text,
            "attachment_urls": attachment_urls,
            "identity_embedding": self.embedder.embed(parsed.searchable_identity),
            "active": True,
            "source_created_at": source_created_at,
        }
        if profile is None:
            profile = CharacterProfile(id=profile_id, **values)
            session.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
        return profile

    async def deactivate_missing(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        source_channel_id: str,
        seen_message_ids: set[str],
    ) -> int:
        profiles = (
            await session.execute(
                select(CharacterProfile).where(
                    CharacterProfile.guild_id == guild_id,
                    CharacterProfile.source_channel_id == source_channel_id,
                    CharacterProfile.active.is_(True),
                )
            )
        ).scalars().all()
        count = 0
        for profile in profiles:
            if profile.source_message_id not in seen_message_ids:
                profile.active = False
                count += 1
        return count

    async def count_profiles(self, session: AsyncSession, guild_id: str | None = None) -> int:
        statement = select(func.count(CharacterProfile.id)).where(CharacterProfile.active.is_(True))
        if guild_id:
            statement = statement.where(CharacterProfile.guild_id == guild_id)
        return int((await session.execute(statement)).scalar_one())

    async def resolve(
        self,
        session: AsyncSession,
        incoming: IncomingMessage,
        scene: SceneConfig,
    ) -> CharacterResolution:
        presentation = self.extract_presentation(incoming.content)
        active = await self._active_identity(session, scene.scene_id, incoming.author_discord_id)

        if active and not presentation.is_presentation:
            return CharacterResolution(
                character_id=active.character_id,
                display_name=active.presented_name,
                status=active.match_status,
                confidence=active.match_confidence,
            )

        if not active and not presentation.is_presentation:
            return CharacterResolution(
                character_id=self._pending_id(incoming, scene),
                display_name=incoming.author_display_name,
                status="NEEDS_PRESENTATION",
                requires_presentation=True,
            )

        candidates = (
            await session.execute(
                select(CharacterProfile).where(
                    CharacterProfile.guild_id == incoming.guild_id,
                    CharacterProfile.owner_discord_user_id == incoming.author_discord_id,
                    CharacterProfile.active.is_(True),
                )
            )
        ).scalars().all()

        matched, confidence, status = self._best_match(
            candidates,
            incoming.content,
            presentation.presented_name,
        )

        if matched is not None:
            presented_name = presentation.presented_name or matched.canonical_name
            resolution = CharacterResolution(
                character_id=matched.id,
                display_name=presented_name,
                status=status,
                confidence=confidence,
            )
        else:
            manual_binding = None
            if presentation.presented_name:
                manual_binding = await self.find_manual_binding(
                    session,
                    guild_id=incoming.guild_id,
                    discord_user_id=incoming.author_discord_id,
                    presented_name=presentation.presented_name,
                )
            if manual_binding is not None:
                resolution = CharacterResolution(
                    character_id=manual_binding.character_id,
                    display_name=presentation.presented_name or manual_binding.character_name,
                    status="MANUAL_BINDING",
                    confidence=1.0,
                )
            elif presentation.presented_name is None:
                return CharacterResolution(
                    character_id=self._pending_id(incoming, scene),
                    display_name=incoming.author_display_name,
                    status="NEEDS_NAME",
                    requires_name=True,
                )
            else:
                resolution = CharacterResolution(
                    character_id=self._provisional_id(
                        incoming,
                        scene,
                        presentation.presented_name,
                    ),
                    display_name=presentation.presented_name,
                    status="PROVISIONAL" if status != "AMBIGUOUS" else status,
                    confidence=confidence,
                )

        await self._set_active_identity(
            session,
            incoming=incoming,
            scene=scene,
            resolution=resolution,
        )
        return resolution

    async def reset_identity(
        self,
        session: AsyncSession,
        *,
        scene_id: str,
        discord_user_id: str,
    ) -> int:
        identities = (
            await session.execute(
                select(SceneCharacterIdentity).where(
                    SceneCharacterIdentity.scene_id == scene_id,
                    SceneCharacterIdentity.discord_user_id == discord_user_id,
                    SceneCharacterIdentity.active.is_(True),
                )
            )
        ).scalars().all()
        for identity in identities:
            identity.active = False
        return len(identities)

    async def _active_identity(
        self,
        session: AsyncSession,
        scene_id: str,
        discord_user_id: str,
    ) -> SceneCharacterIdentity | None:
        return (
            await session.execute(
                select(SceneCharacterIdentity)
                .where(
                    SceneCharacterIdentity.scene_id == scene_id,
                    SceneCharacterIdentity.discord_user_id == discord_user_id,
                    SceneCharacterIdentity.active.is_(True),
                )
                .order_by(SceneCharacterIdentity.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _set_active_identity(
        self,
        session: AsyncSession,
        *,
        incoming: IncomingMessage,
        scene: SceneConfig,
        resolution: CharacterResolution,
    ) -> None:
        await session.execute(
            update(SceneCharacterIdentity)
            .where(
                SceneCharacterIdentity.scene_id == scene.scene_id,
                SceneCharacterIdentity.discord_user_id == incoming.author_discord_id,
                SceneCharacterIdentity.active.is_(True),
            )
            .values(active=False)
        )
        session.add(
            SceneCharacterIdentity(
                guild_id=incoming.guild_id,
                scene_id=scene.scene_id,
                channel_id=incoming.channel_id,
                discord_user_id=incoming.author_discord_id,
                character_id=resolution.character_id,
                presented_name=resolution.display_name,
                presentation_text=incoming.content[:8000],
                match_status=resolution.status,
                match_confidence=resolution.confidence,
                source_message_id=incoming.discord_message_id,
                active=True,
            )
        )
        await session.flush()

    def _best_match(
        self,
        candidates: Sequence[CharacterProfile],
        presentation_text: str,
        presented_name: str | None,
    ) -> tuple[CharacterProfile | None, float, str]:
        if not candidates:
            return None, 0.0, "NO_CANDIDATES"

        normalized_name = self.normalize_name(presented_name or "")
        exact_matches = [
            profile
            for profile in candidates
            if normalized_name
            and normalized_name
            in {self.normalize_name(alias) for alias in [profile.canonical_name, *profile.aliases]}
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], 1.0, "EXACT"
        if len(exact_matches) > 1:
            return None, 1.0, "AMBIGUOUS"

        query_vector = self.embedder.embed(presentation_text)
        scored: list[tuple[float, CharacterProfile]] = []
        lowered = presentation_text.casefold()
        for profile in candidates:
            alias_bonus = 0.0
            for alias in [profile.canonical_name, *profile.aliases]:
                normalized_alias = self.normalize_name(alias)
                if normalized_alias and normalized_alias in self.normalize_name(lowered):
                    alias_bonus = max(alias_bonus, 0.82)
            similarity = self._dot(query_vector, list(profile.identity_embedding or []))
            score = max(alias_bonus, max(0.0, similarity))
            scored.append((score, profile))

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if (
            best_score >= self.settings.character_match_threshold
            and best_score - second_score >= self.settings.character_match_margin
        ):
            return best, best_score, "MATCHED"
        return None, best_score, "AMBIGUOUS" if len(scored) > 1 else "LOW_CONFIDENCE"

    @staticmethod
    def extract_presentation(text: str) -> Presentation:
        presented_name: str | None = None
        for pattern in _PRESENTATION_PATTERNS:
            match = pattern.search(text)
            if match:
                presented_name = CharacterRegistryService.clean_presented_name(match.group(1))
                break

        if presented_name is None:
            plain = _PLAIN_NAME_RE.match(text)
            if plain:
                candidate = CharacterRegistryService.clean_presented_name(plain.group(1))
                # Reject common one-word replies that merely start with a capital letter.
                blocked = {"Привет", "Да", "Нет", "Хорошо", "Ладно", "Спасибо", "Говори"}
                if candidate not in blocked:
                    presented_name = candidate

        lowered_tokens = set(re.findall(r"[а-яёa-z]+", text.casefold()))
        appearance_hits = sum(
            1
            for stem in _APPEARANCE_STEMS
            if any(token.startswith(stem) for token in lowered_tokens)
        )
        has_appearance = appearance_hits >= 2 or (len(text) >= 80 and appearance_hits >= 1)
        return Presentation(
            presented_name=presented_name,
            has_appearance=has_appearance,
            is_presentation=bool(presented_name or has_appearance),
        )

    @staticmethod
    def clean_presented_name(value: str) -> str:
        cleaned = re.split(r"[,.!?;:\n]", value, maxsplit=1)[0]
        return cleaned.strip().strip('"«»* _—–-')[:80]

    @staticmethod
    def normalize_name(value: str) -> str:
        return re.sub(r"[^а-яёa-z0-9]+", "", value.casefold())

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        value = sum(a * b for a, b in zip(left, right, strict=False))
        if math.isnan(value):
            return 0.0
        return float(value)

    def _pending_id(self, incoming: IncomingMessage, scene: SceneConfig) -> str:
        del scene
        return "pending:" + self._digest(
            incoming.guild_id,
            incoming.author_discord_id,
        )

    def _provisional_id(
        self,
        incoming: IncomingMessage,
        scene: SceneConfig,
        presented_name: str,
    ) -> str:
        del scene
        return "provisional:" + self._digest(
            incoming.guild_id,
            incoming.author_discord_id,
            self.normalize_name(presented_name),
        )

    def _digest(self, *parts: str) -> str:
        payload = ":".join((self.settings.pseudonym_secret, *parts)).encode("utf-8")
        return hashlib.blake2b(payload, digest_size=12).hexdigest()

    async def find_manual_binding(
        self,
        session: AsyncSession,
        *,
        guild_id: str,
        discord_user_id: str,
        presented_name: str,
    ) -> CharacterBinding | None:
        normalized = self.normalize_name(presented_name)
        bindings = (
            await session.execute(
                select(CharacterBinding).where(
                    CharacterBinding.guild_id == guild_id,
                    CharacterBinding.discord_user_id == discord_user_id,
                    CharacterBinding.active.is_(True),
                )
            )
        ).scalars().all()
        for binding in bindings:
            if self.normalize_name(binding.character_name) == normalized:
                return binding
        return None
