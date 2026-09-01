"""Interface locations for future search work; no retrieval is implemented."""

from __future__ import annotations

from typing import Protocol, Sequence


class TextRetriever(Protocol):
    def retrieve(self, query: str, *, limit: int) -> Sequence[str]:
        """Return candidate text chunk IDs for a future keyword/vector layer."""
        ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Future injection point; no endpoint or local model is assumed."""
        ...
