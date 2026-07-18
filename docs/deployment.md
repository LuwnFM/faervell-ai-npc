# Развёртывание на VPS

## 1. Подготовка

Нужны Docker Engine, Docker Compose plugin, домен только при внешнем доступе к operational API. Сам Discord-бот принимает события через исходящее Gateway-соединение, поэтому открывать порт 8080 в интернет необязательно.

```bash
git clone https://github.com/YOUR_ORG/faervell-ai-npc.git
cd faervell-ai-npc
cp .env.example .env
nano .env
```

Обязательно измените `POSTGRES_PASSWORD`, пароль в `DATABASE_URL`, `PSEUDONYM_SECRET`, `DISCORD_TOKEN`. Ключ OpenRouter можно оставить пустым для локального режима.

## 2. Запуск

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

Инициализация схемы выполняется автоматически. Затем загрузите знания:

```bash
docker compose exec app faervell-npc ingest data/sources.yaml
```

## 3. Проверка

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
```

В Discord выполните `/stranger scene_enable`, `/stranger character_bind`, `/stranger status`.

## 4. Обновление

```bash
./scripts/backup.sh
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=200 app
```

Изменения ORM после первого запуска должны сопровождаться миграцией. Не удаляйте volume PostgreSQL для обычного обновления.

## 5. Backup и restore

```bash
./scripts/backup.sh
./scripts/restore.sh backups/faervell-YYYYMMDD-HHMMSS.dump
```

Restore перезаписывает содержимое выбранной базы. Перед ним остановите `app` и создайте дополнительную копию.

## 6. Эксплуатационные команды

```bash
docker compose exec app faervell-npc behavior scan --days 30
docker compose exec app faervell-npc decision list
docker compose exec app faervell-npc ingest data/sources.yaml
```

## 7. Секреты и сеть

- `.env` не коммитится.
- Порт 8080 оставьте доступным только localhost/VPN либо закройте firewall.
- GM-only и private источники не должны маркироваться `PUBLIC_*`.
- Поставьте лимит расходов у провайдера и в `PLANNER_DAILY_BUDGET_USD`.
- Проверяйте backup восстановлением, а не только наличием файла.
