#!/usr/bin/env python3
"""Build the compact read-only SQLite index used by the market-price tool."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="economy-items.jsonl")
    parser.add_argument("output", type=Path, help="economy.sqlite3")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.unlink()
    conn = sqlite3.connect(args.output)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE economy_items (
                id TEXT PRIMARY KEY,
                country TEXT NOT NULL,
                country_norm TEXT NOT NULL,
                category TEXT NOT NULL,
                item_name TEXT NOT NULL,
                item_name_norm TEXT NOT NULL,
                price_otn TEXT NOT NULL,
                price_currency TEXT NOT NULL,
                quantity TEXT NOT NULL,
                description TEXT NOT NULL,
                additional_details TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_row INTEGER NOT NULL
            );
            """
        )
        rows = []
        with args.input.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                rows.append(
                    (
                        item["id"], item["country"], item["country"].casefold(), item["category"], item["item_name"],
                        item["item_name"].casefold(), item.get("price_otn", ""),
                        item.get("price_currency", ""), item.get("quantity", ""),
                        item.get("description", ""), item.get("additional_details", ""),
                        item["source_id"], item["source_url"], int(item.get("source_row", 0)),
                    )
                )
                if len(rows) >= 2000:
                    conn.executemany("INSERT INTO economy_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    rows.clear()
        if rows:
            conn.executemany("INSERT INTO economy_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executescript(
            """
            CREATE INDEX economy_items_name ON economy_items(item_name_norm);
            CREATE INDEX economy_items_country_name ON economy_items(country, item_name_norm);
            CREATE INDEX economy_items_country_norm_name ON economy_items(country_norm, item_name_norm);
            CREATE INDEX economy_items_category ON economy_items(category);
            PRAGMA analysis_limit=400;
            PRAGMA optimize;
            """
        )
        conn.commit()
        print(json.dumps({"items": conn.execute("SELECT count(*) FROM economy_items").fetchone()[0], "output": str(args.output)}, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
