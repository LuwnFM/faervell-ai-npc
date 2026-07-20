#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/faervell-ai-npc}"
SERVICE="${SERVICE:-app}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080}"

cd "$APP_DIR"

echo "== Repository =="
git status --short
git fetch origin main
git checkout main
git pull --ff-only origin main

echo "== Source verification =="
python scripts/verify-v1.0.0.py
python -m compileall -q faervell_npc
python -m pytest -q
python - <<'PY'
from faervell_npc.runtime import build_runtime
from faervell_npc.services.v100_hotfix import HOTFIX_VERSION
runtime = build_runtime()
print({
    "hotfix": HOTFIX_VERSION,
    "templates": len(runtime.templates.all()),
    "economy_index": runtime.economy.path.is_file(),
})
assert len(runtime.templates.all()) == 512
assert runtime.economy.path.is_file()
PY

echo "== Container rebuild =="
docker compose build "$SERVICE"
docker compose up -d --no-deps "$SERVICE"
docker compose ps

echo "== Runtime verification =="
for _ in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL/health" >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS "$HEALTH_URL/health"; echo
curl -fsS "$HEALTH_URL/ready"; echo
curl -fsS "$HEALTH_URL/version"; echo

docker compose exec -T "$SERVICE" python - <<'PY'
from faervell_npc.runtime import build_runtime
from faervell_npc.services.v100_hotfix import HOTFIX_VERSION
runtime = build_runtime()
result = {
    "hotfix": HOTFIX_VERSION,
    "templates": len(runtime.templates.all()),
    "economy_index": runtime.economy.path.is_file(),
}
print(result)
assert result["templates"] == 512
assert result["economy_index"] is True
PY

echo "Deployment verified."
