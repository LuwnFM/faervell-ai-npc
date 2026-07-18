from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from faervell_npc.config import get_settings


class SceneLockManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.local_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def lock(self, scene_key: str) -> AsyncIterator[None]:
        redis_lock = self.redis.lock(
            f"faervell:scene-lock:{scene_key}",
            timeout=90,
            blocking_timeout=5,
        )
        acquired = False
        try:
            acquired = bool(await redis_lock.acquire())
        except Exception:
            acquired = False

        if acquired:
            try:
                yield
            finally:
                try:
                    await redis_lock.release()
                except Exception:
                    pass
            return

        async with self.local_locks[scene_key]:
            yield

    async def close(self) -> None:
        await self.redis.aclose()
