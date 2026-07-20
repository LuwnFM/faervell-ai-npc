#!/usr/bin/env python3
"""Read the public Faervell economy workbook into reviewable JSONL.

This uses the normal public ``edit``/``gviz`` read endpoints. It does not
disable JavaScript, forge a session, or modify any spreadsheet. The resulting
JSONL is suitable for an offline review/import job and is intentionally kept
outside PostgreSQL until the source revision is recorded.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

MAIN_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
LINKED_ID_RE = re.compile(r"https://docs\.google\.com/spreadsheets/(?:u/0/)?d/([A-Za-z0-9_-]+)/edit")
GID_RE = re.compile(r"\[\d+,0,\\\"(\d+)")
COUNTRY_RE = re.compile(r"государства\s*[«\"']?([^»\"'!]+)", re.I)


@dataclass(frozen=True, slots=True)
class SheetTab:
    name: str
    gid: str


def fetch(client: httpx.Client, url: str) -> str:
    response = client.get(url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.text


def discover_tabs(client: httpx.Client, workbook_id: str) -> list[SheetTab]:
    html = fetch(client, f"https://docs.google.com/spreadsheets/d/{workbook_id}/edit")
    names = [item.get_text(" ", strip=True) for item in _soup(html).select(".docs-sheet-tab")]
    gids = GID_RE.findall(html)
    if len(names) != len(gids):
        raise RuntimeError(f"sheet metadata mismatch for {workbook_id}: {len(names)} names/{len(gids)} gids")
    return [SheetTab(name, gid) for name, gid in zip(names, gids, strict=True)]


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def discover_linked_workbooks(client: httpx.Client, main_id: str) -> list[str]:
    html = fetch(client, f"https://docs.google.com/spreadsheets/d/{main_id}/edit?gid=154340791")
    ids = {item for item in LINKED_ID_RE.findall(html) if item != main_id}
    return sorted(ids)


def csv_rows(client: httpx.Client, workbook_id: str, gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{workbook_id}/gviz/tq?tqx=out:csv&gid={gid}"
    response = client.get(url, timeout=90, follow_redirects=True)
    response.raise_for_status()
    return [list(row) for row in csv.reader(io.StringIO(response.text))]


def nonempty(row: list[str]) -> list[str]:
    return [str(value or "").replace("\u00a0", " ").strip() for value in row]


def country_from_intro(rows: list[list[str]]) -> str:
    text = " ".join(str(cell or "").replace("\u00a0", " ").strip() for row in rows for cell in row if cell)
    match = COUNTRY_RE.search(text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return "Неизвестное государство"


def header_index(header: list[str], *needles: str) -> int | None:
    for index, value in enumerate(header):
        folded = value.casefold()
        if all(needle.casefold() in folded for needle in needles):
            return index
    return None


def normalize_item(
    *,
    country: str,
    category: str,
    workbook_id: str,
    tab: SheetTab,
    row_number: int,
    header: list[str],
    row: list[str],
) -> dict[str, Any] | None:
    values = nonempty(row)
    name_i = header_index(header, "название", "предмет")
    if name_i is None or name_i >= len(values):
        return None
    name = values[name_i]
    if not name or name.lower() in {"название предмета", "итого", "всего"}:
        return None
    price_i = header_index(header, "стоимость", "отн")
    currency_i = header_index(header, "стоимость", "валют")
    quantity_i = header_index(header, "количество")
    info_i = header_index(header, "информация")
    extra_i = header_index(header, "дополнитель")
    price = values[price_i] if price_i is not None and price_i < len(values) else ""
    currency = values[currency_i] if currency_i is not None and currency_i < len(values) else ""
    quantity = values[quantity_i] if quantity_i is not None and quantity_i < len(values) else ""
    info = values[info_i] if info_i is not None and info_i < len(values) else ""
    extra = values[extra_i] if extra_i is not None and extra_i < len(values) else ""
    identity = "|".join((workbook_id, tab.gid, country, category, name, price, currency, quantity))
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "source_id": f"faervell_economy:{workbook_id}:{tab.gid}",
        "source_url": f"https://docs.google.com/spreadsheets/d/{workbook_id}/edit?gid={tab.gid}",
        "workbook_id": workbook_id,
        "country": country,
        "category": category.strip("💼🍗👗⚔️⛏️📦🎓⛰️🐮⛵️🏠⚗️🏷️ "),
        "sheet": tab.name,
        "item_name": name,
        "price_otn": price,
        "price_currency": currency,
        "quantity": quantity,
        "description": info,
        "additional_details": extra,
        "source_row": row_number,
        "review_status": "IMPORTED_PUBLIC_VIEW",
    }


def extract_workbook(client: httpx.Client, workbook_id: str, limit: int | None) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    tabs = discover_tabs(client, workbook_id)
    if len(tabs) < 3:
        return workbook_id, [], {"workbook_id": workbook_id, "error": "no category sheets"}
    intro = csv_rows(client, workbook_id, tabs[1].gid)
    country = country_from_intro(intro)
    items: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    for tab in tabs[2:]:
        rows = csv_rows(client, workbook_id, tab.gid)
        header: list[str] = []
        for row_number, row in enumerate(rows, start=1):
            values = nonempty(row)
            if not header and any("название предмета" in value.casefold() for value in values):
                header = values
                continue
            if not header:
                continue
            item = normalize_item(
                country=country,
                category=tab.name,
                workbook_id=workbook_id,
                tab=tab,
                row_number=row_number,
                header=header,
                row=row,
            )
            if item is None:
                continue
            items.append(item)
            category_counts[item["category"]] += 1
            if limit is not None and len(items) >= limit:
                return workbook_id, items, {"workbook_id": workbook_id, "country": country, "items": len(items), "categories": dict(category_counts)}
    return workbook_id, items, {"workbook_id": workbook_id, "country": country, "items": len(items), "categories": dict(category_counts)}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook_url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--limit-per-workbook", type=int)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    match = MAIN_ID_RE.search(args.workbook_url)
    if not match:
        parser.error("workbook_url must contain /d/<spreadsheet-id>/")
    main_id = match.group(1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(headers={"User-Agent": "Faervell-NPC/1.0 economy importer"})
    try:
        workbook_ids = discover_linked_workbooks(client, main_id)
        if not workbook_ids:
            raise RuntimeError("no linked public economy workbooks found")
        results: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
            futures = [pool.submit(extract_workbook, client, workbook_id, args.limit_per_workbook) for workbook_id in workbook_ids]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"{result[0]}: {result[2].get('country', '?')} items={len(result[1])}", file=sys.stderr)
        results.sort(key=lambda item: item[0])
        total = 0
        with args.output.open("w", encoding="utf-8", newline="\n") as stream:
            for _, items, _ in results:
                for item in items:
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    total += 1
        manifest = {
            "source_workbook": main_id,
            "source_url": args.workbook_url,
            "linked_workbooks": len(workbook_ids),
            "items": total,
            "mode": "public_view_gviz",
            "workbooks": [meta for _, _, meta in results],
        }
        manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"linked_workbooks": len(workbook_ids), "items": total, "output": str(args.output)}, ensure_ascii=False))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
