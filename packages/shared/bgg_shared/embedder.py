"""Voyage AI embeddings wrapper."""

import os

from bgg_shared.retry import with_retry


class Embedder:
    """Wraps Voyage AI for batch text embeddings with exponential-backoff retry."""

    def __init__(self, api_key: str | None = None, model: str = "voyage-3-lite") -> None:
        import voyageai  # lazy — not available in Lambda
        self._client = voyageai.Client(api_key=api_key or os.environ["VOYAGE_API_KEY"])
        self._model = model

    @with_retry(max_attempts=3, base_delay=1.0)
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Retries on transient failures."""
        result = self._client.embed(texts, model=self._model)
        return result.embeddings
