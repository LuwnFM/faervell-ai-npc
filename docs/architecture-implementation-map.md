# Соответствие архитектуры 1.6 реализации

| Требование архитектуры | Реализация |
|---|---|
| Четыре маршрута CHAT / MECHANICS / LORE / PLANNER | `services/router.py`, `services/orchestrator.py` |
| Одна сущность Странника и профессиональные маски | `SceneConfig.profession_mask_id`, единый `holder_entity_id=traveler_01`, `behavior-pack/profession-masks.yaml` |
| Общая память между масками, но раздельные персонажи | `TravelerMemory.character_id`, `observed_under_mask`, `CharacterBinding` |
| Сырой неизменяемый архив | `conversation_messages` + PostgreSQL trigger `forbid_conversation_message_mutation` |
| Утверждения игроков не становятся каноном | `MemoryPerspective.PLAYER_SAID`, `TrustStatus.UNVERIFIED`, формулировка «Персонаж сообщил…» |
| Раздельные индексы механик и лора | `KnowledgeChunk.corpus`, `KnowledgeService.search`, manifest `data/sources.yaml` |
| Механика бесплатна и точна | маршрут `MECHANICS`, `LoreDisclosureEngine` всегда открывает `Corpus.MECHANICS` |
| Знание, разрешение и цена раскрытия независимы | `KnowledgeHit`, `DisclosureDecision`, `DisclosureExchange`, `LoreDisclosureEngine` |
| Скрытый лор не попадает актёру | `ToolExecutor.search_lore` возвращает только `free_summary` и условие обмена |
| Платный planner вызывается точечно | `IntentRouter`, `LocalPlanner`, `DecisionCacheService`, затем `PlannerService` |
| Модель не пишет в БД напрямую | строгие `ToolRequest`, белый список `ToolExecutor`; SQL модели недоступен |
| Планировщик не пишет художественный пост | два прохода: `PlannerPlan` → tools → `ActorPacket`; RP пишет `ActorService` |
| Квесты — граф зависимостей | `QuestDraft.objectives`, `RuleEngine._has_cycle`, `QuestObjective.depends_on` |
| Рискованные последствия ждут GM | принудительный `gm_approval_required`, статус `PENDING_GM` |
| Единая погода в сцене | детерминированный `get_world_weather` в `ToolExecutor` (MVP) |
| Неизвестное создаёт очередь GM | `KnowledgeGap`, `/stranger status`, behavior scan |
| Характер нельзя переписывать автоматически | `behavior-pack/persona.md` не входит в allowlist behavior patch |
| Ручное самообновление | CLI `behavior scan/validate/apply/rollback` |
| Удачные решения повторно используются без API | `approved-examples.jsonl` + ручной `decision approve` |
| Активные/пассивные ограничения и проверка ответа | prompt blocks в `ActorService`, `OutputGuard`, grounding в `PlannerService` |
| Аудит расходов и действий | `ModelCall`, `AuditLog`, сохранение route/model/ActorPacket/response |
| Защита от повторной обработки | уникальный audit `(action,message_id)` и восстановление обработанного результата |

## Осознанные ограничения MVP

- Инвентарь, баланс и официальный registry предметов возвращают безопасный `NOT_CONNECTED/UNKNOWN`, пока не подключена реальная серверная БД.
- Автоматическая классификация фрагментов вики эвристическая; важный лор необходимо проверить GM.
- Для изменения схемы работающей production-БД нужен Alembic.
- Детерминированная погода заменяется таблицей `world_weather_state`, когда GM-погода будет подключена.
- Полный temporal graph не используется: MVP следует рекомендации PostgreSQL + pgvector.
