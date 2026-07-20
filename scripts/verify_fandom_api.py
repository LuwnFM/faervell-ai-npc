#!/usr/bin/env python3
"""Audit the public Faervell Fandom MediaWiki API without mutating it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def fetch_pages(api: str, *, include_redirects: bool) -> list[dict[str, object]]:
    continuation: dict[str, object] = {}
    pages: list[dict[str, object]] = []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            params: dict[str, object] = {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "generator": "allpages",
                "gapnamespace": 0,
                "gapfilterredir": "all" if include_redirects else "nonredirects",
                "gaplimit": 50,
                "prop": "revisions|info",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "inprop": "url",
                "maxlag": 5,
                **continuation,
            }
            response = client.get(api, params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            batch = (payload.get("query") or {}).get("pages") or []
            if isinstance(batch, dict):
                batch = list(batch.values())
            pages.extend(item for item in batch if isinstance(item, dict))
            continuation = dict(payload.get("continue") or {})
            if not continuation:
                return pages


def fetch_stats(api: str) -> dict[str, int]:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(
            api,
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "meta": "siteinfo",
                "siprop": "statistics",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        stats = ((payload.get("query") or {}).get("statistics") or {})
        return {
            key: int(stats.get(key) or 0)
            for key in ("pages", "articles", "edits", "images")
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="https://faervellrp.fandom.com/ru/api.php")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-readable", type=int, default=689)
    args = parser.parse_args()
    stats = fetch_stats(args.api)
    pages = fetch_pages(args.api, include_redirects=False)
    all_pages = fetch_pages(args.api, include_redirects=True)
    with_content = 0
    empty: list[str] = []
    lengths: list[int] = []
    for page in pages:
        revisions = page.get("revisions") or []
        revision = revisions[0] if revisions and isinstance(revisions[0], dict) else {}
        slots = revision.get("slots") or {}
        main = slots.get("main") if isinstance(slots, dict) else {}
        content = str((main or {}).get("content") or (main or {}).get("*") or "")
        lengths.append(len(content))
        if content.strip():
            with_content += 1
        else:
            empty.append(str(page.get("title") or page.get("pageid")))
    report = {
        "api": args.api,
        "siteinfo_stats": stats,
        "all_namespace0_pages": len(all_pages),
        "redirect_pages": max(0, len(all_pages) - len(pages)),
        "nonredirect_articles": len(pages),
        "readable_articles": with_content,
        "empty_revision_count": len(empty),
        "empty_titles": empty,
        "max_content_chars": max(lengths, default=0),
        "min_content_chars": min(lengths, default=0),
        "status": "OK" if with_content >= args.min_readable else "FAILED",
        "note": "Compare this audit with the wiki statistics endpoint; allpages/nonredirects intentionally excludes redirects and may differ by one due to MediaWiki statistics lag.",
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
