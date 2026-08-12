"""OpenAI-compatible structured knowledge extraction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam
from openai.types.shared_params import ResponseFormatJSONSchema
from pydantic import ValidationError

from sofias_memory.config import Settings
from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    KnowledgeExtractionValidationError,
    validate_extraction_for_chunk,
)
from sofias_memory.schemas.summary import DocumentSummaryOutput

GRAPH_EXTRACTION_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "graph_extraction.v1.md"
)
DOCUMENT_SUMMARY_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "document_summary.v1.md"
)
STRUCTURED_OUTPUT_REPAIR_ATTEMPTS = 1


class KnowledgeExtractionOutputError(RuntimeError):
    """Structured LLM output remained invalid after the one allowed repair."""


class DocumentSummaryOutputError(RuntimeError):
    """Document summary output remained invalid after the one allowed repair."""


class OpenAIKnowledgeExtractionClient:
    """Extract validated chunk knowledge through OpenAI-compatible chat completions."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.llm_model
        self._prompt = GRAPH_EXTRACTION_PROMPT_PATH.read_text(encoding="utf-8")
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction:
        last_error: Exception | None = None
        for attempt in range(STRUCTURED_OUTPUT_REPAIR_ATTEMPTS + 1):
            try:
                raw_output = await self._request_structured_output(
                    chunk_text,
                    repair=attempt > 0,
                )
                extraction = ChunkKnowledgeExtraction.model_validate(json.loads(raw_output))
                return validate_extraction_for_chunk(extraction, chunk_text)
            except (
                json.JSONDecodeError,
                ValidationError,
                KnowledgeExtractionValidationError,
                TypeError,
            ) as exc:
                last_error = exc

        raise KnowledgeExtractionOutputError(
            "Knowledge extraction returned invalid structured output."
        ) from last_error

    async def _request_structured_output(self, chunk_text: str, *, repair: bool) -> str:
        system_prompt = self._prompt
        if repair:
            system_prompt = (
                f"{system_prompt}\n\nThe previous response was invalid. Obey the JSON Schema "
                "strictly and ensure every evidence value is copied verbatim from the chunk."
            )
        messages: list[ChatCompletionSystemMessageParam | ChatCompletionUserMessageParam] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"<untrusted_chunk>\n{chunk_text}\n</untrusted_chunk>",
            },
        ]
        response_format: ResponseFormatJSONSchema = {
            "type": "json_schema",
            "json_schema": {
                "name": "chunk_knowledge_extraction",
                "description": "Retrieval summary and directed knowledge extracted from one chunk.",
                "schema": ChunkKnowledgeExtraction.model_json_schema(by_alias=True),
                "strict": True,
            },
        }
        async with self._semaphore:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format=response_format,
            )
        content = response.choices[0].message.content if response.choices else None
        if not isinstance(content, str):
            raise TypeError("structured response content is missing")
        return content


class OpenAIDocumentSummaryClient:
    """Aggregate ordered chunk summaries through OpenAI-compatible structured output."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.llm_model
        self._prompt = DOCUMENT_SUMMARY_PROMPT_PATH.read_text(encoding="utf-8")
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        last_error: Exception | None = None
        for attempt in range(STRUCTURED_OUTPUT_REPAIR_ATTEMPTS + 1):
            try:
                raw_output = await self._request_structured_output(
                    chunk_summaries,
                    repair=attempt > 0,
                )
                return DocumentSummaryOutput.model_validate(json.loads(raw_output)).summary
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                last_error = exc

        raise DocumentSummaryOutputError(
            "Document summary returned invalid structured output."
        ) from last_error

    async def _request_structured_output(
        self,
        chunk_summaries: Sequence[str],
        *,
        repair: bool,
    ) -> str:
        system_prompt = self._prompt
        if repair:
            system_prompt = (
                f"{system_prompt}\n\nThe previous response was invalid. Obey the JSON Schema "
                "strictly and return one non-empty document summary."
            )
        summary_inputs = "\n\n".join(
            f"Input {index}:\n{summary}" for index, summary in enumerate(chunk_summaries, start=1)
        )
        messages: list[ChatCompletionSystemMessageParam | ChatCompletionUserMessageParam] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"<untrusted_chunk_summaries>\n{summary_inputs}\n</untrusted_chunk_summaries>"
                ),
            },
        ]
        response_format: ResponseFormatJSONSchema = {
            "type": "json_schema",
            "json_schema": {
                "name": "document_summary",
                "description": "Retrieval-ready summary aggregated from ordered chunk summaries.",
                "schema": DocumentSummaryOutput.model_json_schema(),
                "strict": True,
            },
        }
        async with self._semaphore:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format=response_format,
            )
        content = response.choices[0].message.content if response.choices else None
        if not isinstance(content, str):
            raise TypeError("structured response content is missing")
        return content
