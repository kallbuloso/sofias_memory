from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sofias_memory.infrastructure.llm import (
    KnowledgeExtractionOutputError,
    OpenAIKnowledgeExtractionClient,
)


@pytest.mark.asyncio
async def test_knowledge_extraction_repairs_invalid_output_once() -> None:
    client = object.__new__(OpenAIKnowledgeExtractionClient)
    self_relation = (
        '{"summary":"Chunking is deterministic","entities":['
        '{"local_id":"e1","name":"chunking","type":"Concept",'
        '"description":"Chunking is deterministic","aliases":[],"confidence":0.9}],'
        '"relations":[{"source_local_id":"e1","target_local_id":"e1",'
        '"predicate":"is_deterministic","description":"Chunking is deterministic",'
        '"confidence":0.9,"evidence":"chunking is deterministic"}]}'
    )
    valid_relation = (
        '{"summary":"Chunking is deterministic","entities":['
        '{"local_id":"e1","name":"chunking","type":"Concept",'
        '"description":"A splitting process","aliases":[],"confidence":0.9},'
        '{"local_id":"e2","name":"deterministic output","type":"Concept",'
        '"description":"A reproducible output","aliases":[],"confidence":0.8}],'
        '"relations":[{"source_local_id":"e1","target_local_id":"e2",'
        '"predicate":"produces","description":"Chunking produces deterministic output",'
        '"confidence":0.9,"evidence":"chunking produces deterministic output"}]}'
    )
    request = AsyncMock(
        side_effect=[
            self_relation,
            valid_relation,
        ]
    )
    client._request_structured_output = request

    result = await client.extract("chunking produces deterministic output")

    assert result.relations[0].source_local_id == "e1"
    assert result.relations[0].target_local_id == "e2"
    assert request.await_count == 2
    assert request.await_args_list[0].kwargs["repair"] is False
    assert request.await_args_list[1].kwargs["repair"] is True


@pytest.mark.asyncio
async def test_knowledge_extraction_fails_after_one_self_relation_repair() -> None:
    client = object.__new__(OpenAIKnowledgeExtractionClient)
    self_relation = (
        '{"summary":"Neo4j is a projection","entities":['
        '{"local_id":"e1","name":"Neo4j","type":"Technology",'
        '"description":"A graph database","aliases":[],"confidence":0.9}],'
        '"relations":[{"source_local_id":"e1","target_local_id":"e1",'
        '"predicate":"is_reconstructible_projection_of_knowledge_graph",'
        '"description":"Neo4j is a reconstructible projection",'
        '"confidence":0.8,"evidence":"Neo4j is a reconstructible projection"}]}'
    )
    request = AsyncMock(return_value=self_relation)
    client._request_structured_output = request

    with pytest.raises(KnowledgeExtractionOutputError):
        await client.extract("Neo4j is a reconstructible projection")

    assert request.await_count == 2
