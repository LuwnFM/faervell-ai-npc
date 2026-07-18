from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from faervell_npc.config import get_settings
from faervell_npc.services.embeddings import get_embedder


class ApprovedExampleService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()
        self.path = Path(self.settings.behavior_pack_path) / "approved-examples.jsonl"
        self._mtime_ns: int | None = None
        self._examples: list[tuple[dict[str, Any], list[float]]] = []

    def search(self, query: str, *, limit: int = 4) -> list[dict[str, Any]]:
        self._reload_if_needed()
        query_vector = self.embedder.embed(query)
        scored = [
            (self._cosine(query_vector, vector), example)
            for example, vector in self._examples
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {**example, "similarity": round(score, 4)}
            for score, example in scored[:limit]
            if score > 0.08
        ]

    def _reload_if_needed(self) -> None:
        if not self.path.exists():
            self._examples = []
            return
        mtime_ns = self.path.stat().st_mtime_ns
        if self._mtime_ns == mtime_ns:
            return
        loaded: list[tuple[dict[str, Any], list[float]]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            example = json.loads(line)
            text = str(example.get("input_pattern") or example.get("text") or "")
            if text:
                loaded.append((example, self.embedder.embed(text)))
        self._examples = loaded
        self._mtime_ns = mtime_ns

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True)) / max(
            math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)),
            1e-12,
        )
