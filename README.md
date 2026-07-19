# Faervell AI-NPC — «Странник»

Готовый MVP Discord-бота для RP-проекта «Фаервелл». Репозиторий реализует архитектуру 1.6 из материалов проекта: локальное ядро решений, бесплатную модель-актёра, точечную платную эскалацию планировщику, раздельные индексы механик и лора, единое существо с профессиональными масками, непроверенную память игроков и жёсткую серверную проверку любых последствий.

## Что уже реализовано

- Discord-бот на `discord.py` со slash-командами.
- Один Странник с общей памятью между масками `traveler/herbalist/artisan/merchant/guide`.
- Единое физическое присутствие: в каждый момент Странник находится только в одной включённой локации.
- Вероятностные появления, очередь следующей локации и RP-пост при прибытии.
- Осмысленный пинг из другой локации планирует её как следующую цель; тестовые и случайные пинги отбрасываются локальным классификатором.
- Переключаемая spoiler-подсказка под RP-постами о том, что продолжение нужно отправлять пингом или reply.
- Реестр анкет персонажей из Discord-канала с безопасным сопоставлением только среди персонажей конкретного игрока.
- Четыре маршрута: `CHAT`, `MECHANICS`, `LORE`, `PLANNER`.
- PostgreSQL 16 + pgvector, HNSW-индексы, PostgreSQL full-text search.
- Append-only архив сообщений: PostgreSQL-триггер запрещает `UPDATE/DELETE` исходных сообщений.
- Производная память `PLAYER_SAID`, обещания/долги и отношения с конкретным игровым персонажем.
- Локальные эмбеддинги без API. Опционально можно подключить multilingual sentence-transformer.
- Раздельное извлечение механик и лора.
- `LoreDisclosureEngine`: `FREE/USEFUL/VALUABLE/RARE/RESTRICTED`.
- Механики всегда выдаются бесплатно и точно по загруженному источнику.
- Скрытая часть лора не передаётся модели-актёру.
- OpenRouter actor fallback и платный planner fallback.
- Локальный planner для погоды и повторных квестовых вопросов без API.
- Кэш успешных планировочных решений с ручным approve/reject.
- Двухпроходный планировщик: строгий JSON-план → серверные инструменты → строгий `ActorPacket`.
- Белый список инструментов. Модель не получает SQL и не пишет в БД напрямую.
- Серверная проверка квестов: маска, лимит награды, evidence, DAG зависимостей, GM approval.
- Риск `HIGH` и явный `requires_gm_approval` принудительно переводят commit квеста в `PENDING_GM`.
- Дополнительная проверка не допускает факты/числа ActorPacket, не подтверждённые результатами серверных инструментов.
- Детерминированная внутриигровая погода для MVP.
- Output Guard: длина, запрещённые факты, новые числа, современная лексика, OOC-ссылки на ИИ.
- `knowledge_gap` для неизвестных вопросов.
- Ручной версионируемый обновлятор поведения: scan / validate / apply / rollback.
- Docker Compose, health API, CI и unit-тесты.

## Важные границы MVP

1. Инвентарь и официальный серверный реестр предметов пока не подключены. Инструмент `check_inventory` возвращает `NOT_CONNECTED`, поэтому бот не может ложно засчитать сдачу предмета.
2. Квест с предметом создаётся только после поиска evidence в загруженных источниках. Если сущность не подтверждается найденным фрагментом, сервер отклонит квест.
3. Экономические таблицы импортируются как знания. Для автоматического начисления валюты потребуется подключить реальный модуль баланса/инвентаря проекта.
4. Автореплика без API работает, но художественное качество выше с OpenRouter actor.
5. Автоматически определённые уровни ценности лора помечаются `tier_auto_inferred=true`; GM должен просмотреть важные страницы и исправить метаданные.

## Быстрый запуск на VPS

Требования: Docker Engine и Docker Compose plugin.

```bash
git clone https://github.com/YOUR_ORG/faervell-ai-npc.git
cd faervell-ai-npc
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f app
```

Проверка:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
```

Начальная индексация источников:

```bash
docker compose exec app faervell-npc ingest data/sources.yaml
```

Импорт официальной Fandom-вики может занять заметное время: команда использует MediaWiki API, получает до `max_pages` страниц, режет смешанные статьи на фрагменты и отдельно классифицирует механику/лор.

## Настройка Discord

1. Создайте приложение и bot user в Discord Developer Portal.
2. Включите **Message Content Intent** и **Server Members Intent**.
3. Пригласите бота со scope `bot applications.commands`.
4. Минимальные permissions: View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Use Application Commands.
5. Запишите токен в `DISCORD_TOKEN`.
6. Для быстрой синхронизации slash-команд укажите `DISCORD_GUILD_ID`.
7. Укажите GM-роли в `DISCORD_GM_ROLE_IDS=123,456`. Администратор сервера всегда считается GM.
8. При успешном старте в логах появится `Discord application commands synced: ...`. Если slash-команды не появились, GM может написать `!stranger-sync`, а после появления группы использовать `/stranger commands_sync`.

При старте бот автоматически регистрирует доступные текстовые каналы в RP-категориях Фаервелла. Повторная синхронизация:

```text
/stranger locations_sync
```

Отдельный тестовый канал вне RP-категорий можно зарегистрировать вручную:

```text
/stranger scene_enable location:Тестовая локация mask:traveler
/stranger appearance_chance percent:20
/stranger status
```

Странник отвечает только в той включённой сцене, где он сейчас находится, и только на:

- прямое упоминание бота;
- Discord reply на пост, отправленный во время **текущего появления** Странника в этой локации.

Если Странник уже ушёл, ответ на оставшийся старый пост архивируется без реакции, не вызывает возвращение и не планирует перемещение. После нового появления ответы на посты из прошлых визитов также считаются устаревшими.

По умолчанию в конце последнего сообщения ответа добавляется spoiler:

```text
||Чтобы продолжить разговор со Странником, упомяните его в своём сообщении или ответьте на один из его постов.||
```

Переключатели и управление присутствием:

```text
/stranger reply_hint enabled:true
/stranger arrival_announcements enabled:true
/stranger cross_location_summons enabled:true
/stranger move_here
/stranger appear_now
/stranger movement_lock enabled:true
/stranger event_locations enabled:false
/stranger permissions
/stranger travel_clear
```

`move_here` меняет физическую локацию без публичного поста. `appear_now` сразу отправляет видимый RP-пост появления и удобен для тестов. `movement_lock enabled:true` закрепляет Странника в текущем канале и блокирует случайные переходы и призывы из других локаций; `enabled:false` снимает ограничение. Категория ивентов по умолчанию исключена и включается отдельным переключателем.

Если Странника пингуют в другой зарегистрированной локации, бот не отвечает из пустого места. Он локально оценивает сообщение. Осмысленная просьба или обращение ставит локацию следующей целью маршрута; `тест`, пустой пинг, одиночная ссылка и похожий шум только записываются в аудит. На каждом цикле движения запланированный переход имеет шанс `TRAVELER_SUMMON_MOVE_CHANCE`, а остальные локации используют собственный процент `appearance_chance`.

Канал анкет задаётся через `DISCORD_CHARACTER_REGISTRY_CHANNEL_ID`. При первом запуске пустой базы бот сканирует его автоматически; вручную — `/stranger characters_sync`. Перед первым разговором персонаж должен представиться или описать внешность. В речи Странник использует имя, которым персонаж представился, а внутреннюю память при уверенном совпадении связывает с анкетой.

## OpenRouter и расходы

Без `OPENROUTER_API_KEY` система остаётся работоспособной:

- маршрутизация, память, RAG и проверки выполняются локально;
- художественный ответ строится безопасным шаблоном;
- сложные действия не применяются, если их нельзя доказать локально.

Стартовые значения:

```env
ACTOR_MODELS=openrouter/free
PLANNER_MODELS=openai/gpt-5-nano,google/gemini-2.5-flash-lite
PLANNER_DAILY_BUDGET_USD=2.00
```

`ACTOR_MODELS` и `PLANNER_MODELS` — упорядоченные списки fallback-моделей OpenRouter. Перед production проверьте их доступность и поддержку structured outputs. Планировщик вызывается только для действий, квестов, смешанных/неоднозначных запросов и иных случаев, требующих серверных инструментов.

Практическая цель архитектуры — 90–98% сообщений без платного planner API. Бесплатный actor может вызываться чаще, но его можно полностью отключить, оставив локальные шаблоны.

## Источники проекта

Manifest: `data/sources.yaml`.

В него уже внесены:

- официальная энциклопедия мира;
- экономика Фаервелла;
- калькулятор рыночной цены;
- экономический путеводитель Google Sheets;
- экономические зоны и валюты из материалов проекта;
- таблицы механик;
- исходная архитектура 1.6 как GM-only внутренний документ.

Исходные `.txt` из проекта лежат в `data/project_sources/`, а полная архитектура — в `docs/architecture-source.md`.

Дополнительная документация:

- `docs/architecture-implementation-map.md` — соответствие требований архитектуры конкретным модулям;
- `docs/deployment.md` — развёртывание, обновление, backup и restore;
- `docs/presence-and-replies.md` — текущее местоположение, вероятности, пинги из других сцен и spoiler-подсказка;
- `docs/v0.4-release.md` — объединённые исправления production-запуска и правило устаревших reply;
- `docs/behavior-patch-example.yaml` — пример ручного патча поведения.

## Как работает обработка сообщения

```text
Discord message
  → append-only archive
  → character binding + scene context
  → local intent router
  → CHAT: память + actor
  → MECHANICS: точный индекс + actor
  → LORE: индекс + disclosure engine + actor
  → PLANNER: strict plan → server tools → validation → ActorPacket → actor
  → output guard
  → Discord response
  → audit log + relationship update
```

### Непроверенная память

Игроковая реплика сохраняется не как истина мира, а как событие:

```text
«Персонаж сообщил Страннику: ...»
```

При извлечении актёр видит метку перспективы. Он может сказать «ты говорил мне…», но не превращает утверждение игрока в канон, награду, навык или доступ.

### Лор и цена знания

Система отдельно проверяет:

- `KNOWS`: знание есть в доступном корпусе;
- `MAY_DISCLOSE`: его разрешено произнести;
- `DISCLOSURE_PRICE`: требуется ли монета, предмет, услуга, доверие, квест или GM approval.

Скрытый текст не помещается в `ActorPacket`. Актёр получает только безопасное резюме и условие обмена.

### Квесты

Планировщик не создаёт квест напрямую. Он должен:

1. найти механику/лор инструментами;
2. сослаться на реальные `knowledge_id`;
3. предложить `QuestDraft`;
4. пройти Rule Engine;
5. вызвать `commit_quest`;
6. только после статуса `ACTIVE` актёр может озвучить активный квест.

Большой риск или сложные шаблоны переходят в `PENDING_GM`.

## Ручное обновление поведения

Сбор отчёта за 30 дней:

```bash
docker compose exec app faervell-npc behavior scan --days 30 \
  --output data/exports/behavior-scan.json
```

Проверка патча:

```bash
docker compose exec app faervell-npc behavior validate docs/behavior-patch-example.yaml
```

Применение:

```bash
docker compose exec app faervell-npc behavior apply path/to/behavior-patch.yaml
```

Откат:

```bash
docker compose exec app faervell-npc behavior rollback 1.0.0
```

Патч может менять только разрешённые YAML-файлы, добавлять одобренные примеры и регрессионные тесты. `persona.md`/`IDENTITY_CORE` автоматически не переписывается.

### Ручное закрепление удачных платных решений

Безопасные результаты планировщика сохраняются как кандидаты, но не начинают переиспользоваться автоматически. Просмотр:

```bash
docker compose exec app faervell-npc decision list
```

Одобрить по полному fingerprint или уникальному префиксу:

```bash
docker compose exec app faervell-npc decision approve 12ab34cd
```

Отклонить и удалить:

```bash
docker compose exec app faervell-npc decision reject 12ab34cd
```

Кэш не переиспользует пакеты с квестовыми ID, балансом, инвентарём или иным изменяемым состоянием. Такие случаи должны превращаться в одобренный пример/правило через behavior patch.

## Локальная разработка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
docker compose up -d postgres redis
faervell-npc init-db
pytest -q
python -m faervell_npc.main
```

Для нейронных локальных embeddings:

```bash
pip install -e '.[semantic]'
```

И в `.env`:

```env
EMBEDDING_PROVIDER=sentence_transformers
SEMANTIC_MODEL=intfloat/multilingual-e5-small
EMBEDDING_DIMENSIONS=384
```

При смене размерности embeddings нужна новая схема/миграция и повторная индексация.

## Структура

```text
faervell_npc/
  discord_bot.py       Discord Gateway + slash commands
  schemas.py           строгие контракты Planner/Actor/Tools
  models.py            PostgreSQL schema
  services/
    orchestrator.py    четыре маршрута
    router.py          дешёвая локальная классификация
    knowledge.py       hybrid RAG
    disclosure.py      политика раскрытия лора
    memory.py          архив и производная память
    planner.py         платная точечная эскалация
    local_planner.py   бесплатные детерминированные планы
    decision_cache.py  ручной кэш успешных решений
    examples.py        локальный поиск одобренных примеров
    tools.py           белый список серверных действий
    rules.py           квестовые и экономические ограничения
    actor.py           бесплатный RP-актёр + fallback
    guard.py           проверка готового ответа
    ingest.py          импорт источников/Fandom
    behavior.py        ручные behavior patches
behavior-pack/         характер, маски, правила, примеры, тесты
data/sources.yaml      manifest источников
docs/architecture-source.md
```

## Production checklist

- Заменить `POSTGRES_PASSWORD` и ту же парольную часть в `DATABASE_URL` в `.env`.
- Не публиковать `.env`, Discord token и OpenRouter key. Заменить `PSEUDONYM_SECRET` длинным случайным значением.
- Ограничить порт 8080 firewall/reverse proxy.
- Настроить ежедневный backup PostgreSQL (`scripts/backup.sh`) и проверить восстановление (`scripts/restore.sh`).
- Просмотреть авторазмеченный ценный лор.
- Подключить реальные таблицы предметов, валют, рецептов и инвентаря.
- Настроить OpenRouter privacy/ZDR согласно правилам сервера.
- Зафиксировать actor-модель после теста на 50–100 сценах; `openrouter/free` оставить fallback.
- Поставить месячный лимит OpenRouter и `PLANNER_DAILY_BUDGET_USD`.
- Не давать внешним моделям GM-секреты и личные каналы.
- Перед изменением ORM-схемы после первого production-запуска добавить Alembic-миграцию; `create_all` предназначен для свежей установки MVP.

## Лицензия

Код — MIT. Содержимое мира, вики и игровые материалы могут иметь отдельные права владельцев проекта; проверьте их перед публичной публикацией репозитория.

## Реестр игровых персонажей Discord

Бот может импортировать анкеты из канала, заданного в
`DISCORD_CHARACTER_REGISTRY_CHANNEL_ID`. В анкете должен присутствовать пинг владельца и поле
`Имя персонажа`. Поддерживаются многочастные анкеты, размещённые подряд одним анкетологом, а также
текстовые вложения `.txt`, `.md`, `.json`, `.yaml`. Изображения сохраняются как ссылки на исходные
Discord-вложения.

При первой встрече в каждой сцене игрок должен представиться или описать внешность. Сопоставление
выполняется только среди персонажей, привязанных в реестре к Discord-аккаунту этого игрока. Странник
всегда обращается к собеседнику тем именем, которым тот представился, но память и отношения связывает
с канонической анкетой, если совпадение достаточно уверенное. При отсутствии уверенного совпадения
создаётся временная сценическая личность без подмены канонических данных.

Полная анкета хранится сервером для идентификации и аудита, но внешней модели не передаётся. Для
ответов доступны только слова игрока, наблюдаемая внешность и ранее полученная память Странника;
биография, страхи, скрытые навыки и инвентарь не становятся автоматическим знанием NPC.

Команды:

```text
/stranger characters_sync  — полная синхронизация канала анкет (GM)
/stranger identity_reset   — представить в текущей сцене другого персонажа
```

## Strict model and price policy (v0.6)

Production no longer uses the random `openrouter/free` router. The bot sends an explicit,
ordered model allowlist, blocks rejected models by slug, and applies a hard OpenRouter
provider price ceiling to both input and output tokens. DeepSeek V4 Flash is the preferred
paid planner and the first paid actor fallback. See `docs/v0.6-model-policy.md`.
