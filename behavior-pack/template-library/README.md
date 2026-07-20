# Библиотека шаблонов Странника

## Состав
- `approved_examples.stranger.500.jsonl` — 500 готовых RP-шаблонов.
- `quest_archetypes.stranger.json` — 32 MMORPG-архетипа.
- `library_stats.json` — статистика.
- `example_actor_packet.json` — пример подстановки.

## Распределение
- `combat`: 20
- `fallback`: 25
- `lore`: 35
- `memory`: 30
- `quest_dialogue`: 256
- `reward`: 30
- `social`: 50
- `trade`: 40
- `travel`: 14

## Квестовые состояния
Каждый архетип имеет:
`offer`, `accepted`, `reminder`, `progress_partial`, `blocked`,
`completed`, `declined`, `failed`.

## Использование
1. Фильтр по `category`.
2. Для квеста — по `quest_type` и `event`.
3. Учесть `profession_mask`.
4. Проверить все `required_variables`.
5. Подставлять только факты из ActorPacket.
6. `accepted`, `completed`, `failed` публиковать только после серверного `action_result`.

## Главные ограничения
- Шаблон не выбирает цену или награду.
- Шаблон не создаёт предметы и канон.
- Странник предпочитает несмертельные решения.
- Уникальные предметы требуют отдельного основания.
- Слухи и слова игроков не выдаются за подтверждённый факт.
