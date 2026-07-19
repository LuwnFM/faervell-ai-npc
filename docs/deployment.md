# Развёртывание и обновление на VPS

## Первичный запуск

```bash
git clone https://github.com/LuwnFM/faervell-ai-npc.git /opt/faervell-npc/app
cd /opt/faervell-npc/app
cp .env.example .env
chmod 600 .env
# Заполните секреты, не публикуя их в терминале или Git.
bash scripts/deploy-production.sh
```

Проверка:

```bash
curl -fsS http://127.0.0.1:8080/health && echo
curl -fsS http://127.0.0.1:8080/ready && echo
docker compose ps
docker compose logs --tail=200 --no-color app
```

## Обновление до v0.7

```bash
cd /opt/faervell-npc/app
./scripts/backup.sh
git status --short
git pull --ff-only
bash scripts/deploy-production.sh
```

`deploy-production.sh` вызывает `scripts/migrate-v0.7.sh`. Миграция обновляет только несекретные параметры модельной политики, RP-категорий, индексации и startup-lock; токены, API-ключи, пароли и `PSEUDONYM_SECRET` сохраняются.

Не запускайте `docker compose down -v`: флаг `-v` удаляет volumes базы и Redis.

## Discord после обновления

```text
/stranger gm_channel
/stranger locations_sync
/stranger source_ingest
/stranger knowledge_status
/stranger startup_lock_status
/stranger status
```

GM-канал назначается выполнением `/stranger gm_channel` непосредственно в нужном канале. Заявки на квесты и подтверждение знаний появляются там с кнопками решения.

## Проверка OpenRouter

После нового сообщения игрока:

```bash
docker compose logs --tail=250 --no-color app | \
  grep -E "model_call|OpenRouter|ERROR|WARNING" || true
```

Полные результаты также находятся в таблице `model_calls`: модель, HTTP-код, токены, стоимость, latency, причина выбора, безопасные метаданные запроса и тело ошибки.

## Проверка Fandom/RAG

```bash
docker compose exec -T app faervell-npc ingest data/sources.yaml
```

Полный crawl считается успешным только при импорте не менее 500 страниц. При неполном ответе Fandom старая рабочая индексация сохраняется.

## Backup и restore

```bash
./scripts/backup.sh
./scripts/restore.sh backups/faervell-YYYYMMDD-HHMMSS.dump
```
