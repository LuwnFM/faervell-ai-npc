#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${1:-.env}"
test -f "$ENV_FILE" || { echo "v0.7.3 migration: $ENV_FILE not found" >&2; exit 1; }

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    "ACTOR_MODELS": (
        "nvidia/nemotron-3-ultra-550b-a55b:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "openai/gpt-oss-120b:free,"
        "deepseek/deepseek-v4-flash"
    ),
    "ACTOR_MAX_TOKENS": "1000",
    "OPENROUTER_RESPONSE_TIMEOUT_SECONDS": "180",
    "ACTOR_QUALITY_ATTEMPTS": "3",
    "FANDOM_BATCH_SIZE": "40",
    "QUEST_DEFAULT_REWARD_AMOUNT": "5",
    "QUEST_DEFAULT_REWARD_CURRENCY": "местных монет",
}

lines = path.read_text(encoding="utf-8").splitlines()
result: list[str] = []
seen: set[str] = set()
for line in lines:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        result.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")
path.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
PY

chmod 600 "$ENV_FILE"
echo "v0.7.3 migration applied to $ENV_FILE"
