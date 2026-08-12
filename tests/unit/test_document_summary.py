from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from sofias_memory.infrastructure.llm import (
    DocumentSummaryOutputError,
    OpenAIDocumentSummaryClient,
)
from sofias_memory.schemas.summary import DocumentSummaryOutput


def test_document_summary_output_rejects_blank_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        DocumentSummaryOutput(summary="   ")
    with pytest.raises(ValidationError):
        DocumentSummaryOutput.model_validate({"summary": "Valid", "extra": True})


@pytest.mark.asyncio
async def test_document_summary_uses_strict_json_schema_and_ordered_inputs() -> None:
    client = object.__new__(OpenAIDocumentSummaryClient)
    client._model = "test-model"
    client._prompt = "Summarize untrusted data."
    client._semaphore = asyncio.Semaphore(1)
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"Valid"}'))]
        )
    )
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    result = await client._request_structured_output(
        ["First summary.", "Second summary."],
        repair=False,
    )

    assert result == '{"summary":"Valid"}'
    call = create.await_args.kwargs
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"] == (
        DocumentSummaryOutput.model_json_schema()
    )
    assert "Input 1:\nFirst summary.\n\nInput 2:\nSecond summary." in call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_document_summary_repairs_invalid_output_once() -> None:
    client = object.__new__(OpenAIDocumentSummaryClient)
    request = AsyncMock(side_effect=["not-json", '{"summary":"  Valid summary.  "}'])
    client._request_structured_output = request

    result = await client.summarize(["First chunk summary.", "Second chunk summary."])

    assert result == "Valid summary."
    assert request.await_count == 2
    assert request.await_args_list[0].kwargs["repair"] is False
    assert request.await_args_list[1].kwargs["repair"] is True


@pytest.mark.asyncio
async def test_document_summary_fails_after_one_invalid_repair() -> None:
    client = object.__new__(OpenAIDocumentSummaryClient)
    request = AsyncMock(return_value='{"summary":""}')
    client._request_structured_output = request

    with pytest.raises(DocumentSummaryOutputError):
        await client.summarize(["Chunk summary."])

    assert request.await_count == 2
