from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from faervell_npc.config import get_settings

_TOKEN_RE = re.compile(r"[\wа-яё]+", re.IGNORECASE)


class Embedder(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Zero-API deterministic fallback.

    It hashes Russian/Latin word unigrams, word bigrams and character trigrams into
    a fixed vector. It is not a neural semantic model, but gives cheap local retrieval
    and keeps the project functional before an optional sentence-transformer is installed.
    """

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
        features: list[str] = list(tokens)
        features.extend(f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False))
        compact = " ".join(tokens)
        features.extend(compact[i : i + 3] for i in range(max(0, len(compact) - 2)))

        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.dimensions
            sign = 1.0 if (number >> 8) & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dimensions: int) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the semantic extra: pip install '.[semantic]'") from exc
        self.model = SentenceTransformer(model_name)
        actual = self.model.get_sentence_embedding_dimension()
        if actual != dimensions:
            raise ValueError(f"Embedding dimension mismatch: config={dimensions}, model={actual}")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        encoded = self.model.encode(text, normalize_embeddings=True)
        return [float(value) for value in encoded]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.embedding_provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.semantic_model, settings.embedding_dimensions)
    return HashingEmbedder(settings.embedding_dimensions)
