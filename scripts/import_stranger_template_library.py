#!/usr/bin/env python3
"""Import and audit the supplied Stranger template library.

The source archive is data, not a prompt. Every line is checked against the
persona guard before becoming available to runtime selection. Action-result
templates are retained with an explicit review status so they cannot be
published without a verified server result.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ALLOWED_TONES = {"calm", "restrained", "mysterious", "non_aggressive"}
REJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("persona_claims_godhood", re.compile(r"всемог|богом|божеств", re.IGNORECASE)),
    ("persona_starts_attack", re.compile(r"\bя\s+(?:нападу|атакую|убью|украду)\b", re.IGNORECASE)),
    ("persona_invents_facts", re.compile(r"создам\s+из\s+ничего|выдуманн\w*\s+факт", re.IGNORECASE)),
    ("persona_claims_certain_future", re.compile(r"единственно\s+возможн\w*\s+будущ|неизбежно\s+произойд", re.IGNORECASE)),
)


def _audit(record: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    tones = {str(value) for value in (record.get("tone") or [])}
    unknown_tones = sorted(tones - ALLOWED_TONES)
    if unknown_tones:
        reasons.append("unknown_tone:" + ",".join(unknown_tones))

    text = str(record.get("text") or "")
    for reason, pattern in REJECT_PATTERNS:
        if pattern.search(text):
            reasons.append(reason)

    if not record.get("id") or not record.get("event") or not text:
        reasons.append("missing_required_template_fields")
    if not record.get("actor_constraints"):
        reasons.append("missing_actor_constraints")

    if reasons:
        return "REJECTED_PERSONA", reasons
    if bool(record.get("requires_action_result")):
        return "REVIEW_ACTION_RESULT", ["publish_only_after_verified_action_result"]
    return "APPROVED_PERSONA", []


def import_library(archive: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        raw_templates = [
            json.loads(line)
            for line in source.read("stranger_template_library/approved_examples.stranger.500.jsonl")
            .decode("utf-8")
            .splitlines()
            if line.strip()
        ]
        for name in (
            "README.md",
            "quest_archetypes.stranger.json",
            "library_stats.json",
            "example_actor_packet.json",
        ):
            (destination / name).write_bytes(source.read(f"stranger_template_library/{name}"))

    statuses: Counter[str] = Counter()
    audited: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in raw_templates:
        status, reasons = _audit(record)
        enriched = {**record, "library_status": status, "library_reasons": reasons}
        audited.append(enriched)
        statuses[status] += 1
        if status == "REJECTED_PERSONA":
            rejected.append(enriched)

    def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

    write_jsonl(destination / "templates.stranger.jsonl", audited)
    write_jsonl(destination / "rejected.stranger.jsonl", rejected)
    manifest = {
        "schema_version": "1.0.0",
        "source_archive": archive.name,
        "total": len(audited),
        "status_counts": dict(statuses),
        "rejected_ids": [record["id"] for record in rejected],
        "allowed_tones": sorted(ALLOWED_TONES),
        "rules": [reason for reason, _ in REJECT_PATTERNS],
    }
    (destination / "audit.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    manifest = import_library(args.archive, args.destination)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
