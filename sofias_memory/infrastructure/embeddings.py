"""OpenAI-compatible embeddings infrastructure."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from openai import AsyncOpenAI

from sofias_memory.config import Settings


class OpenAIEmbeddingClient:
    """Small OpenAI-compatible embedding client with batched requests."""

    def __init__(self, settings: Settings) -> None:
        embedding_api_key = settings.embedding_api_key or settings.llm_api_key
        self._model = settings.embedding_model
        self._batch_size = settings.embedding_batch_size
        self._max_concurrency = settings.embedding_max_concurrency
        self._client = AsyncOpenAI(
            api_key=embedding_api_key.get_secret_value(),
            base_url=normalized_embedding_base_url(settings.embedding_base_url),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        batches = [
            (index, list(texts[index : index + self._batch_size]))
            for index in range(0, len(texts), self._batch_size)
        ]
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def embed_batch(start_index: int, batch: list[str]) -> tuple[int, list[list[float]]]:
            async with semaphore:
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=batch,
                    encoding_format="float",
                )
            ordered_data = sorted(response.data, key=lambda item: item.index)
            return start_index, [list(item.embedding) for item in ordered_data]

        results = await asyncio.gather(
            *(embed_batch(start_index, batch) for start_index, batch in batches)
        )
        embeddings: list[list[float]] = []
        for _, batch_embeddings in sorted(results, key=lambda item: item[0]):
            embeddings.extend(batch_embeddings)
        return embeddings


def normalized_embedding_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/embeddings"):
        return base[: -len("/embeddings")]
    return base
