#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${1:-.env}"
test -f "$ENV_FILE" || { echo "v0.7 migration: $ENV_FILE not found" >&2; exit 1; }

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    # Preferred order only. The live catalogue supplies all other free text models.
    "ACTOR_MODELS": (
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "openai/gpt-oss-120b:free,"
        "nvidia/nemotron-3-ultra-550b-a55b:free,"
        "deepseek/deepseek-v4-flash"
    ),
    "PLANNER_MODELS": (
        "deepseek/deepseek-v4-flash,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "openai/gpt-oss-120b:free"
    ),
    "MODEL_BLOCKLIST": (
        "openrouter/free,openrouter/auto,openai/gpt-oss-20b,"
        "nvidia/nemotron-nano-9b-v2,laguna-2.1-xs,laguna-2-1-xs"
    ),
    "OPENROUTER_ALLOW_PAID_FALLBACK": "true",
    "OPENROUTER_DYNAMIC_CATALOG": "true",
    "OPENROUTER_CATALOG_TTL_SECONDS": "1800",
    "OPENROUTER_MAX_CATALOG_CANDIDATES": "24",
    "OPENROUTER_MAX_PROMPT_PRICE_PER_MILLION": "0.20",
    "OPENROUTER_MAX_COMPLETION_PRICE_PER_MILLION": "0.20",
    "OPENROUTER_MAX_REQUEST_PRICE_USD": "0.0",
    "OPENROUTER_PLANNER_REASONING_EFFORT": "high",
    "TRAVELER_AUTO_REGISTER_LOCATIONS": "true",
    "TRAVELER_ENFORCE_STARTUP_LOCK": "true",
    "TRAVELER_STARTUP_LOCK_CHANNEL_ID": "1488544832950374481",
    "TRAVELER_RP_CATEGORY_IDS": (
        "682909341300293662,1057679719597879437,1133768572510941276,"
        "1255157727278403614,1426883198327193640,1057717821552984194,"
        "1459852302071631988"
    ),
    "TRAVELER_EVENTS_CATEGORY_ID": "1058403455934398495",
    "TRAVELER_MANUAL_ONLY_CATEGORY_IDS": "730030732185043004,1490668605594013776",
    "KNOWLEDGE_AUTO_INGEST": "true",
    "KNOWLEDGE_MIN_WIKI_DOCUMENTS": "500",
    "KNOWLEDGE_STALE_HOURS": "24",
    "FANDOM_API_CONCURRENCY": "4",
    "DISCORD_MODEL_FOOTER_ENABLED": "true",
    "DISCORD_REGENERATION_LIMIT": "1",
}

raw_lines = path.read_text(encoding="utf-8").splitlines()
result: list[str] = []
seen: set[str] = set()
for raw in raw_lines:
    stripped = raw.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in raw:
        result.append(raw)
        continue
    key = raw.split("=", 1)[0].strip()
    if key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(raw)

for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")

# Add the optional GM route without overwriting a value already set by the owner.
if not any(line.startswith("DISCORD_GM_REVIEW_CHANNEL_ID=") for line in result):
    result.append("DISCORD_GM_REVIEW_CHANNEL_ID=")

path.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
PY

chmod 600 "$ENV_FILE"
echo "v0.7 migration applied to $ENV_FILE"
