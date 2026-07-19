#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${FAERVELL_APP_DIR:-/opt/faervell-npc/app}"
ENV_FILE="${1:-$APP_DIR/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Ошибка: .env не найден: $ENV_FILE" >&2
  exit 1
fi

ACTOR_VALUE='nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-120b:free,nvidia/nemotron-3-ultra-550b-a55b:free,deepseek/deepseek-v4-flash,openai/gpt-oss-120b,mistralai/ministral-14b-2512'
PLANNER_VALUE='deepseek/deepseek-v4-flash,openai/gpt-oss-120b:free,nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-120b,mistralai/ministral-14b-2512'
BLOCKLIST_VALUE='openrouter/free,openrouter/auto,openai/gpt-oss-20b,nvidia/nemotron-nano-9b-v2,laguna-2.1-xs,laguna-2-1-xs'

upsert_env() {
  local key="$1"
  local value="$2"
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
updated: list[str] = []
found = False
for line in lines:
    if line.startswith(key + "="):
        updated.append(f"{key}={value}")
        found = True
    else:
        updated.append(line)
if not found:
    updated.append(f"{key}={value}")
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
}

# Replace the old random router and old planner defaults. Existing explicit custom lists stay intact.
current_actor="$(sed -n 's/^ACTOR_MODELS=//p' "$ENV_FILE" | head -n1)"
current_planner="$(sed -n 's/^PLANNER_MODELS=//p' "$ENV_FILE" | head -n1)"
OLD_V06_ACTOR='nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-120b:free,nvidia/nemotron-3-ultra-550b-a55b:free,openai/gpt-oss-120b,mistralai/ministral-14b-2512'
OLD_V06_PLANNER='openai/gpt-oss-120b:free,nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-120b,mistralai/ministral-14b-2512'
if [[ -z "$current_actor" || "$current_actor" == "openrouter/free" || "$current_actor" == "$OLD_V06_ACTOR" ]]; then
  upsert_env ACTOR_MODELS "$ACTOR_VALUE"
fi
if [[ -z "$current_planner" || "$current_planner" == "openai/gpt-5-nano,google/gemini-2.5-flash-lite" || "$current_planner" == "$OLD_V06_PLANNER" ]]; then
  upsert_env PLANNER_MODELS "$PLANNER_VALUE"
fi

upsert_env MODEL_BLOCKLIST "$BLOCKLIST_VALUE"
upsert_env OPENROUTER_ALLOW_PAID_FALLBACK true
upsert_env OPENROUTER_MAX_PROMPT_PRICE_PER_MILLION 0.20
upsert_env OPENROUTER_MAX_COMPLETION_PRICE_PER_MILLION 0.20
upsert_env OPENROUTER_MAX_REQUEST_PRICE_USD 0.0
upsert_env OPENROUTER_PLANNER_REASONING_EFFORT high
chmod 600 "$ENV_FILE"

echo "OpenRouter model policy v0.6 записана в $ENV_FILE"
