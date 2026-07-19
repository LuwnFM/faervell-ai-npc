#!/usr/bin/env bash
set -Eeuo pipefail
ENV_FILE="${1:-/opt/faervell-npc/app/.env}"
test -f "$ENV_FILE" || { echo "v0.7.4 migration: $ENV_FILE not found" >&2; exit 1; }
# No secret or database changes are required. The migration exists so production
# deployments record the release and can validate the synchronized architecture source.
APP_DIR="$(cd "$(dirname "$ENV_FILE")" && pwd)"
test -f "$APP_DIR/docs/architecture-source.md" || {
  echo "v0.7.4 migration: docs/architecture-source.md is missing" >&2
  exit 1
}
echo "v0.7.4 architecture source validated: $APP_DIR/docs/architecture-source.md"
