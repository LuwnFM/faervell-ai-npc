from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EconomyPrice:
    country: str
    category: str
    item_name: str
    price_otn: str
    price_currency: str
    quantity: str
    description: str
    source_url: str


class EconomyService:
    """Read-only local index for exact prices from the public economy books."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("data/economy/economy.sqlite3")

    def _search_sync(self, item: str, country: str | None, limit: int) -> list[EconomyPrice]:
        if not self.path.is_file():
            return []
        folded = " ".join(item.casefold().split())
        connection = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
        try:
            params: list[object] = [f"%{folded}%"]
            query = (
                "SELECT country,category,item_name,price_otn,price_currency,quantity,description,source_url "
                "FROM economy_items WHERE item_name_norm LIKE ?"
            )
            if country:
                query += " AND country_norm LIKE ?"
                params.append(f"%{country.casefold()}%")
            query += " ORDER BY CASE WHEN item_name_norm = ? THEN 0 ELSE 1 END, length(item_name) LIMIT ?"
            params.extend([folded, max(1, min(limit, 20))])
            rows = connection.execute(query, params).fetchall()
            return [EconomyPrice(*row) for row in rows]
        finally:
            connection.close()

    async def search_prices(
        self, item: str, *, country: str | None = None, limit: int = 5
    ) -> list[EconomyPrice]:
        return await asyncio.to_thread(self._search_sync, item, country, limit)
