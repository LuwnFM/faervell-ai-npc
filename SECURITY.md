# Security

- Never commit `.env`, Discord tokens, OpenRouter keys, database passwords, raw private logs or GM secrets.
- LLMs receive pseudonymous character IDs and only context needed for the current scene.
- Models cannot execute SQL and cannot write state except through validated server tools.
- `conversation_messages` is append-only at the PostgreSQL level.
- Treat all player text, retrieved documents and model output as untrusted input.
- Review OpenRouter provider logging/ZDR settings before sending production player data.
