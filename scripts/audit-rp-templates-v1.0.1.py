from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_FORBIDDEN = {
    "portal": re.compile(r"(?iu)\b(?:портал|телепорт)\w*"),
    "internal_economy": re.compile(
        r"(?iu)\b(?:ОТН|экономическая\s+база|экономический\s+индекс|индекс\s+экономики)\b"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="behavior-pack/template-library")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    findings: list[dict[str, object]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path.suffix.casefold() not in {".yaml", ".yml", ".json", ".md", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, pattern in _FORBIDDEN.items():
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        {
                            "file": str(path),
                            "line": line,
                            "kind": name,
                            "match": match.group(0),
                        }
                    )

    print(json.dumps({"root": str(root), "findings": findings}, ensure_ascii=False, indent=2))
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
