# Russian synonym fallback index

`russian_synonyms.sqlite3` is a compact, read-only query-expansion index built
from the user-provided *Словарь синонимов русского языка* under the editorship
of L. G. Babenko.

It contains:

- 4,821 parsed synonym groups;
- 25,534 normalized searchable terms;
- the complete extracted dictionary text in 182 read-only source chunks for
  audit and future parser improvements;
- a small Faervell domain-alias group for ruler, quest, and location intents.

The dictionary is not a lore source. Structured synonym terms are used only when
the ordinary PostgreSQL FTS + pgvector result is weak. Expanded terms are
reranked against the original entity and intent, and dictionary entries are
never exposed as world facts or citations. Raw source chunks are not injected
into actor prompts.
