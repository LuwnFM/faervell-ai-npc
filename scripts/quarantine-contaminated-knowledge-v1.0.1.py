from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "sexual_discord_fragment",
        re.compile(
            r"(?iu)(?:ты\s+сама\s+мне\s+на\s+член|"
            r"я\s+тебя\s+не\s+насиловал|потрахал|потрахались)"
        ),
    ),
    (
        "discord_debuff",
        re.compile(r"(?iu)(?:вам\s+снято\s*-?\d+\s+морал|дебафф|любитель\s+пушистых)"),
    ),
    (
        "nested_retrieval_dump",
        re.compile(r"(?iu)официальные\s+сведения\s+об\s+объекте"),
    ),
)


@dataclass(frozen=True, slots=True)
class Match:
    chunk_id: str
    source_id: str
    title: str
    reasons: tuple[str, ...]
    preview: str


def _dsn() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise SystemExit("DATABASE_URL is not set")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _reasons(content: str) -> tuple[str, ...]:
    reasons = [name for name, pattern in _PATTERNS if pattern.search(content)]
    if content.casefold().count("официальные сведения об объекте") > 1:
        reasons.append("multiple_nested_retrieval_dumps")
    if len(re.findall(r"(?iu)\bлокация\s*:", content)) > 1:
        reasons.append("multiple_location_chunks")
    return tuple(dict.fromkeys(reasons))


async def run(*, apply: bool, report: str | None) -> int:
    connection = await asyncpg.connect(_dsn())
    try:
        rows = await connection.fetch(
            """
            SELECT id::text, source_id, title, content
            FROM knowledge_chunks
            WHERE access <> 'GM_ONLY'
              AND (
                    content ILIKE '%ты сама мне на член%'
                 OR content ILIKE '%я тебя не насиловал%'
                 OR content ILIKE '%потрахал%'
                 OR content ILIKE '%вам снято%морал%'
                 OR content ILIKE '%дебафф%'
                 OR content ILIKE '%любитель пушистых%'
                 OR content ILIKE '%официальные сведения об объекте%'
              )
            ORDER BY source_id, title, id
            """
        )
        matches: list[Match] = []
        for row in rows:
            content = str(row["content"] or "")
            reasons = _reasons(content)
            if not reasons:
                continue
            preview = " ".join(content.split())[:240]
            matches.append(
                Match(
                    chunk_id=str(row["id"]),
                    source_id=str(row["source_id"] or ""),
                    title=str(row["title"] or ""),
                    reasons=reasons,
                    preview=preview,
                )
            )

        payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "apply" if apply else "dry-run",
            "matches": [
                {
                    "chunk_id": item.chunk_id,
                    "source_id": item.source_id,
                    "title": item.title,
                    "reasons": list(item.reasons),
                    "preview": item.preview,
                }
                for item in matches
            ],
        }

        if apply and matches:
            async with connection.transaction():
                for item in matches:
                    await connection.execute(
                        """
                        UPDATE knowledge_chunks
                        SET access = 'GM_ONLY',
                            metadata_json = (
                                COALESCE(metadata_json, '{}'::json)::jsonb
                                || jsonb_build_object(
                                    'quarantined_by', 'v1.0.1-retrieval-safety',
                                    'quarantined_at', $2::text,
                                    'quarantine_reasons', $3::jsonb
                                )
                            )::json
                        WHERE id::text = $1
                        """,
                        item.chunk_id,
                        payload["generated_at"],
                        json.dumps(list(item.reasons), ensure_ascii=False),
                    )

        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        print(rendered)
        if report:
            await asyncio.to_thread(
                Path(report).write_text, rendered + "\n", encoding="utf-8"
            )
        return 0
    finally:
        await connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find contaminated knowledge chunks. Default mode is dry-run; "
            "--apply marks matches GM_ONLY without deleting the raw archive."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report")
    arguments = parser.parse_args()
    return asyncio.run(run(apply=arguments.apply, report=arguments.report))


if __name__ == "__main__":
    raise SystemExit(main())
