# Экономический индекс Фаервелла

`economy.sqlite3` — локальный read-only индекс публичных книг экономики, собранных из 24 связанных таблиц путеводителя. В индексе 144 768 позиций (6 032 на государство) и исходная ссылка каждой позиции.

Обновление выполняется вне бота:

```powershell
python scripts/import_faervell_economy.py "https://docs.google.com/spreadsheets/d/1qOqm_5noHKOsa2sWxSuW3JnGL39YNbafOo_HPdmOFXQ/edit" --output work/economy-items.jsonl
python scripts/build_economy_index.py work/economy-items.jsonl data/economy/economy.sqlite3
```

Бот использует индекс только для точных запросов цены. Если позиция не найдена, он сообщает об отсутствии точных данных и не выдумывает цену. Исходные Google-таблицы не изменяются.
