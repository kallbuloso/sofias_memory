from __future__ import annotations

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    ExtractedEntity,
    ExtractedRelation,
    KnowledgeExtractionValidationError,
    canonical_entity_key,
    validate_extraction_for_chunk,
)


def entity(local_id: str, name: str = "PostgreSQL") -> ExtractedEntity:
    return ExtractedEntity(
        local_id=local_id,
        name=name,
        type="Technology",
        description=f"{name} is described in the chunk.",
        aliases=[],
        confidence=0.9,
    )


def test_structured_knowledge_validates_local_graph_and_literal_evidence() -> None:
    extraction = ChunkKnowledgeExtraction(
        summary="PostgreSQL supports Sofias Memory.",
        entities=[entity("e1"), entity("e2", "Sofias Memory")],
        relations=[
            ExtractedRelation(
                source_local_id="e1",
                target_local_id="e2",
                predicate="supports",
                description="PostgreSQL supports Sofias Memory.",
                confidence=0.8,
                evidence="PostgreSQL supports Sofias Memory",
            )
        ],
    )

    assert (
        validate_extraction_for_chunk(
            extraction,
            "PostgreSQL supports Sofias Memory with durable storage.",
        )
        is extraction
    )

    with pytest.raises(KnowledgeExtractionValidationError, match="literal"):
        validate_extraction_for_chunk(extraction, "No matching quotation is present.")

    with pytest.raises(ValidationError, match="local_id"):
        ChunkKnowledgeExtraction(
            summary="Invalid duplicate IDs.",
            entities=[entity("e1"), entity("e1", "Sofias Memory")],
            relations=[],
        )

    with pytest.raises(ValidationError, match="existing local entities"):
        ChunkKnowledgeExtraction(
            summary="Invalid endpoint.",
            entities=[entity("e1")],
            relations=[
                ExtractedRelation(
                    source_local_id="e1",
                    target_local_id="missing",
                    predicate="supports",
                    description="A relation with a missing endpoint.",
                    confidence=0.8,
                    evidence="PostgreSQL",
                )
            ],
        )

    with pytest.raises(ValidationError, match="distinct entities"):
        ChunkKnowledgeExtraction(
            summary="Invalid self relation.",
            entities=[entity("e1")],
            relations=[
                ExtractedRelation(
                    source_local_id="e1",
                    target_local_id="e1",
                    predicate="is_deterministic",
                    description="Chunking is deterministic.",
                    confidence=0.8,
                    evidence="chunking is deterministic",
                )
            ],
        )

    with pytest.raises(ValidationError):
        ExtractedEntity(
            local_id="e1",
            name="PostgreSQL",
            type="Technology",
            description="Database.",
            aliases=[],
            confidence=1.1,
        )


def test_canonical_entity_key_is_conservative_and_unicode_normalized() -> None:
    assert canonical_entity_key("Technology", "PostgreSQL") == canonical_entity_key(
        "technology",
        " PostgreSQL ",
    )
    assert canonical_entity_key("Company", "OpenAI") != canonical_entity_key(
        "Product",
        "OpenAI API",
    )
