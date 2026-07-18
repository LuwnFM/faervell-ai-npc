from __future__ import annotations

import asyncio

import uvicorn

from faervell_npc.api import create_app
from faervell_npc.config import get_settings
from faervell_npc.db import close_db, init_db
from faervell_npc.discord_bot import FaervellBot
from faervell_npc.runtime import build_runtime


async def run() -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        await init_db()

    runtime = build_runtime()
    api = create_app(runtime, manage_runtime=False, initialize_schema=False)
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host="0.0.0.0",
            port=8080,
            log_level=settings.log_level.casefold(),
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve(), name="faervell-api")
    bot: FaervellBot | None = None
    bot_task: asyncio.Task[None] | None = None

    if settings.discord_token:
        bot = FaervellBot(runtime)
        bot_task = asyncio.create_task(bot.start(settings.discord_token), name="faervell-discord")

    tasks = [server_task] + ([bot_task] if bot_task else [])
    try:
        await asyncio.gather(*tasks)
    finally:
        if bot and not bot.is_closed():
            await bot.close()
        server.should_exit = True
        await runtime.close()
        await close_db()


if __name__ == "__main__":
    asyncio.run(run())
