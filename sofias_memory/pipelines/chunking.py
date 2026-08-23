"""Deterministic text chunking for cognify."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

import tiktoken
from tiktoken import Encoding

CHUNK_ALGORITHM_VERSION = "1"
CHUNK_HASH_SEPARATOR = "\x1f"
CHUNK_ID_NAMESPACE = uuid5(NAMESPACE_URL, "sofias-memory:chunk:v1")


@dataclass(frozen=True)
class TextChunk:
    """A stable chunk slice of a normalized document."""

    id: UUID
    ordinal: int
    text: str
    content_sha256: str
    token_count: int
    start_char: int
    end_char: int
    section_path: tuple[str, ...]


@lru_cache(maxsize=8)
def _encoding_for_model(embedding_model: str) -> Encoding:
    """Resolve a tiktoken encoding once per model name.

    ``tiktoken.encoding_for_model`` re-reads and re-parses the BPE ranks on
    every call (~1s), and the resulting ``Encoding`` is immutable and
    stateless, so building it once per process is both safe and materially
    cheaper -- notably now that a ``CognifyService`` is constructed at
    application startup rather than per request (SM-510).
    """

    try:
        return tiktoken.encoding_for_model(embedding_model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


class TextTokenizer:
    """Small tiktoken wrapper tied to the configured embedding model."""

    def __init__(self, embedding_model: str) -> None:
        self._encoding = _encoding_for_model(embedding_model)

    @property
    def encoding(self) -> Encoding:
        return self._encoding

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text))


def chunk_document_text(
    text: str,
    *,
    document_id: UUID,
    generation: int,
    tokenizer: TextTokenizer,
    max_tokens: int,
    overlap_tokens: int,
    min_tokens: int,
) -> list[TextChunk]:
    """Split normalized document text into stable, offset-preserving chunks."""

    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be non-negative and less than max_tokens")
    if min_tokens <= 0 or min_tokens > max_tokens:
        raise ValueError("min_tokens must be between 1 and max_tokens")

    spans = _chunk_spans(
        text,
        tokenizer=tokenizer,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    spans = _merge_small_tail(
        text,
        spans,
        tokenizer=tokenizer,
        max_tokens=max_tokens,
        min_tokens=min_tokens,
    )
    return _materialize_chunks(
        text,
        spans,
        document_id=document_id,
        generation=generation,
        tokenizer=tokenizer,
    )


def document_token_count(text: str, tokenizer: TextTokenizer) -> int:
    return tokenizer.count(text)


def chunk_content_hash(text: str) -> str:
    payload = f"{CHUNK_ALGORITHM_VERSION}{CHUNK_HASH_SEPARATOR}{text}"
    return sha256(payload.encode("utf-8")).hexdigest()


def _chunk_spans(
    text: str,
    *,
    tokenizer: TextTokenizer,
    max_tokens: int,
    overlap_tokens: int,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = _next_non_empty_start(text, 0)
    while start < len(text):
        max_end = _max_end_for_token_limit(text, start, tokenizer=tokenizer, max_tokens=max_tokens)
        if max_end <= start:
            break

        end = len(text) if max_end == len(text) else _preferred_cut(text, start, max_end)
        if end <= start:
            end = max_end
        if text[start:end]:
            spans.append((start, end))

        if end >= len(text):
            break

        next_start = _overlap_start(
            text,
            start=start,
            end=end,
            tokenizer=tokenizer,
            overlap_tokens=overlap_tokens,
        )
        if next_start <= start:
            next_start = end
        start = _next_non_empty_start(text, next_start)

    return spans


def _max_end_for_token_limit(
    text: str,
    start: int,
    *,
    tokenizer: TextTokenizer,
    max_tokens: int,
) -> int:
    if tokenizer.count(text[start:]) <= max_tokens:
        return len(text)

    low = start + 1
    high = len(text)
    best = low
    while low <= high:
        mid = (low + high) // 2
        if tokenizer.count(text[start:mid]) <= max_tokens:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _preferred_cut(text: str, start: int, max_end: int) -> int:
    for delimiter in ("\n\n", "\n"):
        position = text.rfind(delimiter, start + 1, max_end + 1)
        if position > start:
            return position + len(delimiter)
    return max_end


def _overlap_start(
    text: str,
    *,
    start: int,
    end: int,
    tokenizer: TextTokenizer,
    overlap_tokens: int,
) -> int:
    if overlap_tokens == 0:
        return end
    if tokenizer.count(text[start:end]) <= overlap_tokens:
        return end

    low = start
    high = end
    best = end
    while low <= high:
        mid = (low + high) // 2
        if tokenizer.count(text[mid:end]) <= overlap_tokens:
            best = mid
            high = mid - 1
        else:
            low = mid + 1
    return best


def _merge_small_tail(
    text: str,
    spans: Sequence[tuple[int, int]],
    *,
    tokenizer: TextTokenizer,
    max_tokens: int,
    min_tokens: int,
) -> list[tuple[int, int]]:
    merged = list(spans)
    if len(merged) < 2:
        return merged
    last_start, last_end = merged[-1]
    if tokenizer.count(text[last_start:last_end]) >= min_tokens:
        return merged
    previous_start, _ = merged[-2]
    if tokenizer.count(text[previous_start:last_end]) <= max_tokens:
        merged[-2:] = [(previous_start, last_end)]
    return merged


def _materialize_chunks(
    text: str,
    spans: Sequence[tuple[int, int]],
    *,
    document_id: UUID,
    generation: int,
    tokenizer: TextTokenizer,
) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    for ordinal, (start, end) in enumerate(spans):
        chunk_text = text[start:end]
        if not chunk_text:
            continue
        content_sha256 = chunk_content_hash(chunk_text)
        chunks.append(
            TextChunk(
                id=uuid5(
                    CHUNK_ID_NAMESPACE,
                    f"{document_id}:{generation}:{ordinal}:{content_sha256}",
                ),
                ordinal=ordinal,
                text=chunk_text,
                content_sha256=content_sha256,
                token_count=tokenizer.count(chunk_text),
                start_char=start,
                end_char=end,
                section_path=(),
            )
        )
    return chunks


def _next_non_empty_start(text: str, start: int) -> int:
    while start < len(text) and not text[start]:
        start += 1
    return start
