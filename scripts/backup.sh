#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
mkdir -p backups
stamp=$(date -u +%Y%m%d-%H%M%S)
user=${POSTGRES_USER:-faervell}
db=${POSTGRES_DB:-faervell}
output="backups/faervell-${stamp}.dump"
docker compose exec -T postgres pg_dump -U "$user" -d "$db" -Fc > "$output"
echo "$output"
