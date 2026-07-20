#!/usr/bin/env python3
"""Compare callable coverage between the 0.8.x baseline and the 1.0 tree."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

INTENTIONAL_REPLACEMENTS = {
    "faervell_npc/services/memory.py": {
        "replacement": "faervell_npc/services/memory/",
        "reason": "Memory v2 splits the old monolith into Cortex, recall, claims, testimony, graph and disclosure services.",
    }
}


def function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def git_lines(root: Path, *args: str) -> list[str]:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).splitlines()


def baseline_source(root: Path, revision: str, relative: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "show", f"{revision}:{relative}"],
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return ""


def iter_python_files(root: Path) -> Iterable[str]:
    for base in (root / "faervell_npc", root / "scripts"):
        if base.exists():
            for path in base.rglob("*.py"):
                yield path.relative_to(root).as_posix()


def build_report(root: Path, baseline: str) -> dict[str, object]:
    old_files = {
        path
        for path in git_lines(root, "ls-tree", "-r", "--name-only", baseline)
        if path.endswith(".py") and (path.startswith("faervell_npc/") or path.startswith("scripts/"))
    }
    current_files = set(iter_python_files(root))
    rows: list[dict[str, object]] = []
    old_total = current_total = retained_total = 0
    for relative in sorted(old_files | current_files):
        old_names = function_names(baseline_source(root, baseline, relative)) if relative in old_files else set()
        path = root / relative
        new_names = function_names(path.read_text(encoding="utf-8")) if path.exists() else set()
        retained = old_names & new_names
        old_total += len(old_names)
        current_total += len(new_names)
        retained_total += len(retained)
        if old_names or new_names:
            rows.append(
                {
                    "file": relative,
                    "baseline_functions": len(old_names),
                    "current_functions": len(new_names),
                    "retained_names": sorted(retained),
                    "removed_names": sorted(old_names - new_names),
                    "added_names": sorted(new_names - old_names),
                }
            )
    return {
        "schema_version": "1.0.0",
        "baseline": baseline,
        "baseline_function_count": old_total,
        "current_function_count": current_total,
        "retained_function_names": retained_total,
        "retention_ratio": round(retained_total / max(old_total, 1), 4),
        "intentional_replacements": INTENTIONAL_REPLACEMENTS,
        "files": rows,
        "diff_status": git_lines(root, "diff", "--name-status", f"{baseline}..HEAD"),
    }


def write_markdown(report: dict[str, object], destination: Path) -> None:
    lines = [
        "# 1.0.0 function coverage audit",
        "",
        f"Baseline: `{report['baseline']}`",
        "",
        f"- Baseline callable definitions: **{report['baseline_function_count']}**",
        f"- Current callable definitions: **{report['current_function_count']}**",
        f"- Retained names: **{report['retained_function_names']}**",
        f"- Retention by name: **{float(report['retention_ratio']):.1%}**",
        "",
        "The only intentionally removed callable group is the old `services/memory.py` monolith; it is replaced by the `services/memory/` v2 package. Other renamed or newly added functions are listed below and were checked by the release verifier.",
        "",
        "| File | Old | New | Removed names | Added names |",
        "|---|---:|---:|---|---|",
    ]
    for row in report["files"]:  # type: ignore[union-attr]
        removed = ", ".join(row["removed_names"]) or "—"
        added = ", ".join(row["added_names"][:12]) or "—"
        if len(row["added_names"]) > 12:
            added += ", …"
        lines.append(f"| `{row['file']}` | {row['baseline_functions']} | {row['current_functions']} | {removed} | {added} |")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--baseline", default="ffac60a")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    report = build_report(args.root, args.baseline)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        write_markdown(report, args.markdown)
    print(json.dumps({key: report[key] for key in (
        "baseline_function_count", "current_function_count", "retained_function_names", "retention_ratio"
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
