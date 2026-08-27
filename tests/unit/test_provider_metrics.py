"""Unit tests for safe LLM/embedding provider observability (SM-516 SS
22-25, 62): tokens only from real ``response.usage``, never fabricated;
provider failures never leak exception text/prompt/response body; embedding
counts only, never vectors/text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import OpenAIRagAnswerClient
from sofias_memory.observability.logging import clear_log_context, configure_logging

SECRET_PROMPT = "PROMPT_SECRET_SENTINEL"
SECRET_RESPONSE_BODY = "RESPONSE_BODY_SECRET_SENTINEL"


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()
    httpx_logger.setLevel(previous_httpx_level)


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def make_rag_client(create: AsyncMock) -> OpenAIRagAnswerClient:
    client = object.__new__(OpenAIRagAnswerClient)
    client._model = "test-model"  # type: ignore[attr-defined]
    client._prompt = "Answer using only the given context."  # type: ignore[attr-defined]
    client._semaphore = asyncio.Semaphore(1)  # type: ignore[attr-defined]
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client


def make_embedding_client(create: AsyncMock) -> OpenAIEmbeddingClient:
    client = object.__new__(OpenAIEmbeddingClient)
    client._model = "test-embedding-model"  # type: ignore[attr-defined]
    client._batch_size = 100  # type: ignore[attr-defined]
    client._max_concurrency = 4  # type: ignore[attr-defined]
    client._client = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=create)
    )
    return client


@pytest.mark.asyncio
async def test_rag_answer_logs_tokens_from_real_usage(log_stream: StringIO) -> None:
    usage = SimpleNamespace(prompt_tokens=42, completion_tokens=8, total_tokens=50)
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="An answer."))],
            usage=usage,
        )
    )
    client = make_rag_client(create)

    await client.answer(SECRET_PROMPT, SECRET_RESPONSE_BODY)

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "llm_request_completed"]
    assert record["operation"] == "rag_answer"
    assert record["model"] == "test-model"
    assert record["prompt_tokens"] == 42
    assert record["completion_tokens"] == 8
    assert record["total_tokens"] == 50
    serialized = json.dumps(record)
    assert SECRET_PROMPT not in serialized
    assert SECRET_RESPONSE_BODY not in serialized
    assert "An answer." not in serialized


@pytest.mark.asyncio
async def test_rag_answer_no_usage_omits_token_fields(log_stream: StringIO) -> None:
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="An answer."))],
            usage=None,
        )
    )
    client = make_rag_client(create)

    await client.answer("q", "c")

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "llm_request_completed"]
    assert "prompt_tokens" not in record
    assert "completion_tokens" not in record
    assert "total_tokens" not in record


@pytest.mark.asyncio
async def test_rag_answer_provider_failure_logs_safely_never_reraises_swallowed(
    log_stream: StringIO,
) -> None:
    create = AsyncMock(side_effect=RuntimeError(f"provider exploded: {SECRET_RESPONSE_BODY}"))
    client = make_rag_client(create)

    with pytest.raises(RuntimeError):
        await client.answer("q", "c")

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "provider_request_failed"]
    assert record["operation"] == "rag_answer"
    assert record["provider_kind"] == "openai_compatible"
    assert record["exception_type"] == "RuntimeError"
    serialized = json.dumps(record)
    assert SECRET_RESPONSE_BODY not in serialized
    assert "provider exploded" not in serialized


@pytest.mark.asyncio
async def test_embedding_request_logs_counts_never_vectors(log_stream: StringIO) -> None:
    secret_vector = [0.123456, 0.654321, 0.999999]
    create = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(index=0, embedding=secret_vector),
                SimpleNamespace(index=1, embedding=secret_vector),
            ],
            usage=SimpleNamespace(total_tokens=12),
        )
    )
    client = make_embedding_client(create)

    result = await client.embed_texts(["EMBEDDING_SENTINEL one", "EMBEDDING_SENTINEL two"])

    assert result == [secret_vector, secret_vector]
    [record] = [
        r for r in read_log_records(log_stream) if r["event"] == "embedding_request_completed"
    ]
    assert record["model"] == "test-embedding-model"
    assert record["input_count"] == 2
    assert record["embedding_count"] == 2
    assert record["total_tokens"] == 12
    serialized = json.dumps(record)
    assert "0.123456" not in serialized
    assert "EMBEDDING_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_embedding_request_failure_logs_safely(log_stream: StringIO) -> None:
    create = AsyncMock(side_effect=RuntimeError("EMBEDDING_PROVIDER_SECRET_SENTINEL"))
    client = make_embedding_client(create)

    with pytest.raises(RuntimeError):
        await client.embed_texts(["one"])

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "provider_request_failed"]
    assert record["operation"] == "embedding"
    assert record["exception_type"] == "RuntimeError"
    assert "EMBEDDING_PROVIDER_SECRET_SENTINEL" not in json.dumps(record)


@pytest.mark.asyncio
async def test_embed_texts_empty_input_never_calls_provider_or_logs(log_stream: StringIO) -> None:
    create = AsyncMock()
    client = make_embedding_client(create)

    result = await client.embed_texts([])

    assert result == []
    create.assert_not_awaited()
    assert read_log_records(log_stream) == []
