from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"(?iu)[а-яёa-z0-9-]{2,}")

_QUERY_STOPWORDS = {
    "а",
    "без",
    "бы",
    "был",
    "была",
    "были",
    "быть",
    "в",
    "вам",
    "вас",
    "во",
    "вот",
    "все",
    "вы",
    "где",
    "да",
    "дай",
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
    "расскажи",
    "с",
    "скажи",
    "со",
    "что",
    "это",
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

# These are domain aliases, not claims about world lore. They only normalize intent.
_DOMAIN_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"(?iu)\b(?:корол|правител|монарх|государ|властител|глава\s+государств)\w*"),
        ("правитель", "король", "монарх", "глава государства"),
    ),
    (
        re.compile(r"(?iu)\b(?:квест|задани|поручени|работ|дело)\w*"),
        ("квест", "задание", "поручение"),
    ),
    (
        re.compile(r"(?iu)\b(?:где|располож|местонахожд)\w*"),
        ("расположение", "местонахождение", "регион"),
    ),
)

# Conservative spelling aliases for stable world entities. The mapping changes only
# the search form; it never injects an answer into the actor packet.
_SPELLING_ALIASES = {
    "ивэлтин": "ивелтин",
    "ивельтин": "ивелтин",
    "ивелтине": "ивелтин",
    "ивелтина": "ивелтин",
    "ивелтинский": "ивелтин",
    "ивелтинского": "ивелтин",
}


@dataclass(frozen=True, slots=True)
class SynonymExpansion:
    original_query: str
    canonical_query: str
    expanded_query: str
    added_terms: tuple[str, ...]
    matched_group_ids: tuple[int, ...]


class SynonymLexicon:
    """Read-only Russian synonym lookup used only after ordinary retrieval is weak.

    The dictionary is deliberately separate from lore/mechanics corpora. Its entries
    expand a player's wording; they are never returned as facts and never cited as lore.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        return self.path.is_file()

    @staticmethod
    def normalize(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value.casefold().replace("ё", "е"))
        without_accents = "".join(
            char for char in decomposed if unicodedata.category(char) != "Mn"
        )
        return " ".join(re.sub(r"[^а-яa-z0-9 -]+", " ", without_accents).split())

    @classmethod
    def light_stem(cls, value: str) -> str:
        normalized = cls.normalize(value)
        if " " in normalized or len(normalized) < 5:
            return normalized
        for suffix in _SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 4:
                return normalized[: -len(suffix)]
        return normalized

    @classmethod
    def canonicalize_query(cls, query: str) -> str:
        words = query.split()
        canonical: list[str] = []
        for word in words:
            bare = cls.normalize(word)
            replacement = _SPELLING_ALIASES.get(bare)
            canonical.append(replacement if replacement is not None else word)
        return " ".join(canonical)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.resolve()}?mode=ro&immutable=1",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def diagnostics(self) -> dict[str, object]:
        if not self.available:
            return {"available": False, "path": str(self.path)}
        with self._connect() as connection:
            groups = int(connection.execute("SELECT COUNT(*) FROM synonym_groups").fetchone()[0])
            terms = int(connection.execute("SELECT COUNT(*) FROM synonym_terms").fetchone()[0])
            source_row = connection.execute(
                "SELECT value FROM metadata WHERE key='source'"
            ).fetchone()
            source_chunks = int(
                connection.execute(
                    "SELECT COUNT(*) FROM dictionary_source_chunks"
                ).fetchone()[0]
            )
        return {
            "available": True,
            "path": str(self.path),
            "groups": groups,
            "terms": terms,
            "source_chunks": source_chunks,
            "source": str(source_row[0]) if source_row else None,
        }

    def _lookup(
        self,
        connection: sqlite3.Connection,
        token: str,
        *,
        max_groups: int = 3,
    ) -> list[sqlite3.Row]:
        normalized = self.normalize(token)
        if not normalized or normalized in _QUERY_STOPWORDS:
            return []
        stem = self.light_stem(normalized)
        return list(
            connection.execute(
                """
                SELECT DISTINCT
                    g.id AS group_id,
                    g.domain AS domain,
                    g.dominant AS dominant,
                    matched.is_dominant AS matched_is_dominant
                FROM synonym_terms AS matched
                JOIN synonym_groups AS g ON g.id = matched.group_id
                WHERE matched.term_norm = ?
                   OR (
                        length(?) >= 4
                        AND matched.term_norm LIKE ?
                        AND length(matched.term_norm) - length(?) BETWEEN 0 AND 5
                   )
                ORDER BY
                    CASE WHEN g.domain LIKE 'Faervell:%' THEN 0 ELSE 1 END,
                    matched.is_dominant DESC,
                    g.id
                LIMIT ?
                """,
                (normalized, stem, f"{stem}%", stem, max_groups),
            )
        )

    @staticmethod
    def _group_terms(
        connection: sqlite3.Connection,
        group_id: int,
        *,
        limit: int = 6,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT term
            FROM synonym_terms
            WHERE group_id = ?
            ORDER BY is_dominant DESC, rank ASC
            LIMIT ?
            """,
            (group_id, limit),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def expand(
        self,
        query: str,
        *,
        max_added_terms: int = 12,
        max_groups_per_token: int = 2,
    ) -> SynonymExpansion:
        canonical = self.canonicalize_query(query)
        if not self.available:
            return SynonymExpansion(query, canonical, canonical, (), ())

        additions: list[str] = []
        groups: list[int] = []
        normalized_query = self.normalize(canonical)

        for pattern, terms in _DOMAIN_EXPANSIONS:
            if pattern.search(normalized_query):
                for term in terms:
                    if term not in additions:
                        additions.append(term)

        tokens = _TOKEN_RE.findall(canonical)
        with self._connect() as connection:
            for raw_token in tokens:
                normalized = self.normalize(raw_token)
                if len(normalized) < 4 or normalized in _QUERY_STOPWORDS:
                    continue
                rows = self._lookup(
                    connection,
                    raw_token,
                    max_groups=max_groups_per_token,
                )
                for row in rows:
                    group_id = int(row["group_id"])
                    if group_id not in groups:
                        groups.append(group_id)
                    for term in self._group_terms(connection, group_id):
                        clean = self.normalize(term)
                        if (
                            clean
                            and clean != normalized
                            and clean not in additions
                            and len(additions) < max_added_terms
                        ):
                            additions.append(clean)
                if len(additions) >= max_added_terms:
                    break

        expanded = " ".join([canonical, *additions]).strip()
        return SynonymExpansion(
            original_query=query,
            canonical_query=canonical,
            expanded_query=expanded,
            added_terms=tuple(additions),
            matched_group_ids=tuple(groups),
        )
