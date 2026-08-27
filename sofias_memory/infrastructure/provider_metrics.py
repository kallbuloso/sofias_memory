"""Safe OpenAI-compatible provider observability (SM-516 SS 22-25).

Shared by the LLM (`llm.py`) and embedding (`embeddings.py`) clients. Never
logs a prompt, message, request/response body, or embedding vector -- only
a bounded operation name, the model, timing, and (when the provider actually
returns it) token/input/output counts. Instrumentation only: never retries,
never duplicates a provider call, never changes classification.
"""

from __future__ import annotations

from typing import Any

from sofias_memory.observability.logging import get_logger

logger = get_logger(__name__)

PROVIDER_KIND_OPENAI_COMPATIBLE = "openai_compatible"


def log_llm_request_completed(
    *,
    operation: str,
    model: str,
    duration_ms: float,
    usage: Any | None,
) -> None:
    fields: dict[str, object] = {
        "operation": operation,
        "model": model,
        "duration_ms": duration_ms,
    }
    if usage is not None:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if isinstance(prompt_tokens, int):
            fields["prompt_tokens"] = prompt_tokens
        if isinstance(completion_tokens, int):
            fields["completion_tokens"] = completion_tokens
        if isinstance(total_tokens, int):
            fields["total_tokens"] = total_tokens
    logger.info("llm_request_completed", **fields)


def log_embedding_request_completed(
    *,
    model: str,
    input_count: int,
    embedding_count: int,
    duration_ms: float,
    usage: Any | None,
) -> None:
    fields: dict[str, object] = {
        "model": model,
        "input_count": input_count,
        "embedding_count": embedding_count,
        "duration_ms": duration_ms,
    }
    if usage is not None:
        total_tokens = getattr(usage, "total_tokens", None)
        if isinstance(total_tokens, int):
            fields["total_tokens"] = total_tokens
    logger.info("embedding_request_completed", **fields)


def log_provider_request_failed(
    *,
    operation: str,
    exception: BaseException,
    duration_ms: float,
) -> None:
    """Never logs ``str(exception)``/``repr(exception)``, a response body, a
    request payload, a prompt, or a base URL -- ``exception_type`` only
    (SS 23)."""

    logger.warning(
        "provider_request_failed",
        provider_kind=PROVIDER_KIND_OPENAI_COMPATIBLE,
        operation=operation,
        exception_type=type(exception).__name__,
        duration_ms=duration_ms,
    )


__all__ = [
    "PROVIDER_KIND_OPENAI_COMPATIBLE",
    "log_embedding_request_completed",
    "log_llm_request_completed",
    "log_provider_request_failed",
]
