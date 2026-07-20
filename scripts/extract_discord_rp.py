#!/usr/bin/env python3
"""Stream RP candidates from the Discord exporter SQLite archive.

The archive is treated as an immutable source. This command never updates the
SQLite file and never sends messages to Discord. It emits reviewable JSONL so
the v1.0 memory writer can ingest approved scenes in bounded batches.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_INCLUDE = (
    "природн",
    "локац",
    "полночь-рп",
    "ивент-вознесение",
    "🔷rp информация🔷",
    "roleplay",
)
DEFAULT_EXCLUDE = (
    "оос",
    "флуд",
    "администрац",
    "боты",
    "логи-команд",
    "логи-входов",
    "голосов",
)
OOC_RE = re.compile(r"(?iu)(?:^|\s)(?:\(\(|//|\[ooc\]|ooc:|оос:)")
ACTION_RE = re.compile(r"[*«»—]|\n\s*[-–—]\s*|\b(?:он|она|они|мужчина|женщина)\s+(?:сказал|сделал|пош[её]л|взглянул)", re.I)


def _contains(path: str, needles: Iterable[str]) -> bool:
    folded = path.casefold()
    return any(needle.casefold() in folded for needle in needles)


def is_rp_candidate(
    *,
    channel_path: str,
    content: str,
    min_chars: int,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> tuple[bool, float, list[str]]:
    text = " ".join(content.split())
    if len(text) < min_chars or not text:
        return False, 0.0, ["too_short"]
    if _contains(channel_path, exclude) and not _contains(channel_path, ("природн", "локац")):
        return False, 0.0, ["excluded_channel"]
    if OOC_RE.search(content):
        return False, 0.0, ["ooc_marker"]

    score = 0.0
    reasons: list[str] = []
    if _contains(channel_path, include):
        score += 0.62
        reasons.append("rp_location_channel")
    if len(text) >= 500:
        score += 0.18
        reasons.append("long_narrative")
    elif len(text) >= 260:
        score += 0.12
        reasons.append("narrative_length")
    if ACTION_RE.search(content):
        score += 0.16
        reasons.append("narrative_action")
    if "\n" in content:
        score += 0.04
        reasons.append("structured_post")
    return score >= 0.62, min(score, 1.0), reasons or ["weak_signal"]


def iter_candidates(
    conn: sqlite3.Connection,
    *,
    min_chars: int,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    limit: int | None,
) -> Iterable[dict[str, Any]]:
    query = """
        SELECT id, guild_id, channel_id, channel_path, author_id,
               author_name, author_display_name, created_at, edited_at,
               content, reference_message_id, attachments_json, embeds_json
        FROM messages
        ORDER BY created_at, id
    """
    emitted = 0
    for row in conn.execute(query):
        ok, score, reasons = is_rp_candidate(
            channel_path=str(row[3] or ""),
            content=str(row[9] or ""),
            min_chars=min_chars,
            include=include,
            exclude=exclude,
        )
        if not ok:
            continue
        yield {
            "source_message_id": str(row[0]),
            "guild_id": str(row[1]),
            "channel_id": str(row[2]),
            "channel_path": str(row[3] or ""),
            "author_id": str(row[4] or ""),
            "author_name": str(row[5] or row[6] or ""),
            "created_at": str(row[7] or ""),
            "edited_at": row[8],
            "content": str(row[9] or ""),
            "reference_message_id": row[10],
            "attachments_json": row[11],
            "embeds_json": row[12],
            "scope_type": "SHARED_EVENT",
            "candidate_score": round(score, 3),
            "classifier_reasons": reasons,
            "review_status": "PENDING",
        }
        emitted += 1
        if limit is not None and emitted >= limit:
            return


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path, help="discord_archive.sqlite3")
    parser.add_argument("--output", type=Path, help="JSONL output; omit for stats only")
    parser.add_argument("--min-chars", type=int, default=180)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample", type=int, default=0, help="print first N candidates")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"SQLite archive not found: {args.db}")

    include = tuple(args.include or DEFAULT_INCLUDE)
    exclude = tuple(args.exclude or DEFAULT_EXCLUDE)
    conn = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    out = args.output.open("w", encoding="utf-8", newline="\n") if args.output else None
    count = 0
    try:
        for item in iter_candidates(
            conn,
            min_chars=max(40, args.min_chars),
            include=include,
            exclude=exclude,
            limit=args.limit,
        ):
            count += 1
            if args.sample and count <= args.sample:
                print(json.dumps(item, ensure_ascii=False)[:1200])
            if out:
                out.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            if count % 1000 == 0:
                print(f"extracted={count}", file=sys.stderr)
    finally:
        if out:
            out.close()
        conn.close()
    print(f"RP candidates: {count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
