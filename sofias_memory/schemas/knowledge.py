"""Validated structured knowledge extracted from one chunk."""

from __future__ import annotations

import re
import unicodedata
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractedEntity(BaseModel):
    """Chunk-local entity emitted by structured LLM extraction."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    local_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: str = Field(alias="type", min_length=1)
    description: str = Field(min_length=1)
    aliases: list[str]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("local_id", "name", "entity_type", "description")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class ExtractedRelation(BaseModel):
    """Directed relation between chunk-local entities."""

    model_config = ConfigDict(extra="forbid")

    source_local_id: str = Field(min_length=1)
    target_local_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    description: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)

    @field_validator(
        "source_local_id",
        "target_local_id",
        "predicate",
        "description",
        "evidence",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("predicate")
    @classmethod
    def require_normalizable_predicate(cls, value: str) -> str:
        if not normalize_relation_predicate(value):
            raise ValueError("predicate must contain a word character")
        return value


class ChunkKnowledgeExtraction(BaseModel):
    """Complete validated extraction contract for one chunk."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("summary must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_local_graph(self) -> Self:
        entities_by_local_id: dict[str, ExtractedEntity] = {}
        for entity in self.entities:
            if entity.local_id in entities_by_local_id:
                raise ValueError("entity local_id values must be unique")
            entities_by_local_id[entity.local_id] = entity

        for relation in self.relations:
            source = entities_by_local_id.get(relation.source_local_id)
            target = entities_by_local_id.get(relation.target_local_id)
            if source is None or target is None:
                raise ValueError("relations must reference existing local entities")
            if relation.source_local_id == relation.target_local_id:
                raise ValueError("relations must connect two distinct entities")
        return self


class KnowledgeExtractionValidationError(ValueError):
    """Safe validation error for chunk-dependent extraction invariants."""


def validate_extraction_for_chunk(
    extraction: ChunkKnowledgeExtraction,
    chunk_text: str,
) -> ChunkKnowledgeExtraction:
    """Validate invariants that require the source chunk text."""

    for entity in extraction.entities:
        if any(alias not in chunk_text for alias in entity.aliases):
            raise KnowledgeExtractionValidationError(
                "entity aliases must be literal chunk substrings"
            )
    for relation in extraction.relations:
        if relation.evidence not in chunk_text:
            raise KnowledgeExtractionValidationError(
                "relation evidence must be a literal chunk substring"
            )
    return extraction


def canonical_entity_key(entity_type: str, name: str) -> str:
    """Build the conservative dataset-wide canonical identity for an entity."""

    normalized_type = _normalize_identity_text(entity_type)
    normalized_name = _normalize_identity_text(name)
    if not normalized_type or not normalized_name:
        raise ValueError("entity type and name must not be blank")
    return f"{normalized_type}:{normalized_name}"


def normalize_relation_predicate(predicate: str) -> str:
    """Normalize an extracted predicate to conservative snake_case."""

    normalized = unicodedata.normalize("NFKC", predicate).strip().casefold()
    normalized = re.sub(r"[\s-]+", "_", normalized)
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE)
    return normalized.strip("_")


def _normalize_identity_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()
