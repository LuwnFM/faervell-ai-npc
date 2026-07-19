#!/usr/bin/env bash
set -Eeuo pipefail
ENV_FILE="${1:-/opt/faervell-npc/app/.env}"
test -f "$ENV_FILE" || { echo "v0.8.0 migration: $ENV_FILE not found" >&2; exit 1; }
APP_DIR="$(cd "$(dirname "$ENV_FILE")" && pwd)"
test -f "$APP_DIR/docs/stranger-persona-source.md" || {
  echo "v0.8.0 migration: persona source is missing" >&2
  exit 1
}
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
updates = {
    "DISCORD_WORLD_NEWS_FORUM_ID": "1320514974396973186",
    "DISCORD_WORLD_NEWS_AUTHOR_IDS": "855605848105287711,331217779019481089",
    "DISCORD_LOCATION_SYNC_TIMEOUT_SECONDS": "480",
    "DISCORD_LOCATION_CHANNEL_TIMEOUT_SECONDS": "90",
    "DISCORD_LOCATION_MAX_THREADS": "500",
    "DISCORD_LOCATION_MAX_MESSAGES": "2000",
    "DISCORD_LOCATION_SYNC_CONCURRENCY": "3",
    "DISCORD_LOCATION_LONG_MESSAGE_MIN_CHARS": "220",
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
python3 "$APP_DIR/scripts/verify-v0.8.0.py" "$APP_DIR"
echo "v0.8.0 migration complete: persona and Discord knowledge settings validated"
