#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${FAERVELL_APP_DIR:-/opt/faervell-npc/app}"
cd "$APP_DIR"

test -f docker-compose.yml || {
  echo "Ошибка: docker-compose.yml не найден в $APP_DIR" >&2
  exit 1
}
test -f .env || {
  echo "Ошибка: production .env не найден в $APP_DIR" >&2
  exit 1
}

chmod 600 .env
if [[ -f scripts/migrate-v0.7.sh ]]; then
  bash scripts/migrate-v0.7.sh "$APP_DIR/.env"
fi
if [[ -f scripts/migrate-v0.7.3.sh ]]; then
  bash scripts/migrate-v0.7.3.sh "$APP_DIR/.env"
fi

docker compose config >/dev/null
docker compose up -d --build

for _ in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8080/ready >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "=== VERSION ==="
git log -1 --oneline || true

echo "=== CONTAINERS ==="
docker compose ps

echo "=== HEALTH ==="
curl -fsS http://127.0.0.1:8080/health && echo

echo "=== READY ==="
curl -fsS http://127.0.0.1:8080/ready && echo

echo "=== APP LOGS ==="
docker compose logs --tail=180 --no-color app
