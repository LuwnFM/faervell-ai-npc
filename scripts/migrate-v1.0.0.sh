#!/usr/bin/env bash
set -Eeuo pipefail
ENV_FILE="${1:-/opt/faervell-npc/app/.env}"
test -f "$ENV_FILE" || { echo "v1.0.0 migration: $ENV_FILE not found" >&2; exit 1; }
APP_DIR="$(cd "$(dirname "$ENV_FILE")" && pwd)"
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    "DISCORD_CHARACTER_REGISTRY_CHANNEL_ID": "707461395209256982",
    "CHARACTER_REGISTRY_AUTO_SYNC_ENABLED": "true",
    "CHARACTER_REGISTRY_SYNC_INTERVAL_HOURS": "48",
    "TRAVELER_MEMORY_V2_ENABLED": "true",
    "TRAVELER_MEMORY_V2_WRITE_ENABLED": "true",
    "TRAVELER_MEMORY_V2_READ_ENABLED": "true",
    "MEMORY_LLM_UPDATE_ENABLED": "false",
    "MEMORY_BACKGROUND_LIFE_ENABLED": "false",
    "MEMORY_MULTIPLE_SOURCES_CONFIRM": "false",
    "TRAVELER_SCENE_SETTLE_SECONDS": "900",
    "TRAVELER_ENFORCE_STARTUP_LOCK": "true",
    "TRAVELER_STARTUP_LOCK_CHANNEL_ID": "1488544832950374481",
    "MODEL_CONTEXT_LENGTH": "8192",
    "KNOWLEDGE_MIN_WIKI_DOCUMENTS": "669",
}
lines = path.read_text(encoding="utf-8").splitlines()
result = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
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
python3 "$APP_DIR/scripts/verify-v1.0.0.py" "$APP_DIR"
echo "v1.0.0 migration complete: memory, economy, character refresh and presence limits validated"
