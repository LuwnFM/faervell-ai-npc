#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 backups/file.dump" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
backup=$1
[[ -f "$backup" ]] || { echo "Backup not found: $backup" >&2; exit 2; }
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
user=${POSTGRES_USER:-faervell}
db=${POSTGRES_DB:-faervell}
docker compose stop app
cat "$backup" | docker compose exec -T postgres pg_restore -U "$user" -d "$db" --clean --if-exists --no-owner
docker compose start app
echo "Restored $backup into $db"
