"""Minimal graph projection port and command contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID

GRAPH_PROJECTION_SCHEMA_VERSION = 1

type ProjectionAggregateType = Literal[
    "entity",
    "chunk",
    "relation",
    "entity_mention",
    "chunk_next",
]
type ProjectionOperation = Literal["upsert", "delete"]
type ProjectionPropertyValue = str | int | float | None

ALLOWED_AGGREGATE_TYPES = frozenset(("entity", "chunk", "relation", "entity_mention", "chunk_next"))
ALLOWED_OPERATIONS = frozenset(("upsert", "delete"))

ENTITY_PROPERTIES = frozenset(
    ("id", "dataset_id", "name", "entity_type", "description", "importance_weight", "generation")
)
CHUNK_PROPERTIES = frozenset(
    ("id", "dataset_id", "source_id", "document_id", "ordinal", "generation")
)
RELATION_PROPERTIES = frozenset(
    (
        "relation_id",
        "predicate",
        "description",
        "confidence",
        "importance_weight",
        "generation",
    )
)
ENTITY_MENTION_PROPERTIES = frozenset(("mention_id", "confidence"))
CHUNK_NEXT_PROPERTIES: frozenset[str] = frozenset()


class ProjectionValidationError(ValueError):
    """Projection command payload does not match the frozen ADR contract."""


@dataclass(frozen=True)
class ProjectionCommand:
    """One validated Neo4j projection command snapshot."""

    schema_version: int
    aggregate_type: ProjectionAggregateType
    operation: ProjectionOperation
    dataset_id: str
    aggregate_id: str
    identity: dict[str, str]
    endpoints: dict[str, str]
    properties: dict[str, ProjectionPropertyValue]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ProjectionCommand:
        """Build and validate one command from graph_outbox.payload JSONB."""

        return projection_command_from_payload(payload)

    def to_payload(self) -> dict[str, object]:
        """Serialize the validated ADR-0008 command snapshot for JSONB storage."""

        command = validate_projection_command(self)
        return {
            "schema_version": command.schema_version,
            "aggregate_type": command.aggregate_type,
            "operation": command.operation,
            "dataset_id": command.dataset_id,
            "aggregate_id": command.aggregate_id,
            "identity": dict(command.identity),
            "endpoints": dict(command.endpoints),
            "properties": dict(command.properties),
        }


class GraphProjectionPort(Protocol):
    """Port for applying one graph projection command."""

    async def apply(self, command: ProjectionCommand) -> None:
        """Apply one idempotent graph projection command."""


def projection_command_from_payload(payload: Mapping[str, object]) -> ProjectionCommand:
    """Build and validate one projection command from a JSON-like mapping."""

    schema_version = _schema_version(payload.get("schema_version"))
    aggregate_type = _aggregate_type(payload.get("aggregate_type"))
    operation = _operation(payload.get("operation"))
    dataset_id = _string_value(payload.get("dataset_id"), "dataset_id")
    aggregate_id = _string_value(payload.get("aggregate_id"), "aggregate_id")
    identity = _string_mapping(payload.get("identity"), "identity")
    endpoints = _string_mapping(payload.get("endpoints", {}), "endpoints")
    properties = _properties(payload.get("properties", {}), aggregate_type, operation)

    command = ProjectionCommand(
        schema_version=schema_version,
        aggregate_type=aggregate_type,
        operation=operation,
        dataset_id=dataset_id,
        aggregate_id=aggregate_id,
        identity=identity,
        endpoints=endpoints,
        properties=properties,
    )
    return validate_projection_command(command)


def validate_projection_command(command: ProjectionCommand) -> ProjectionCommand:
    """Validate a command object before any Neo4j write is attempted."""

    if command.schema_version != GRAPH_PROJECTION_SCHEMA_VERSION:
        raise ProjectionValidationError("unsupported projection schema_version")
    if command.aggregate_type not in ALLOWED_AGGREGATE_TYPES:
        raise ProjectionValidationError("unsupported projection aggregate_type")
    if command.operation not in ALLOWED_OPERATIONS:
        raise ProjectionValidationError("unsupported projection operation")

    if command.aggregate_type in {"entity", "chunk"}:
        _require_identity(command, ("id",))
        _require_no_endpoints(command)
        _require_aggregate_identity(command, "id")
    elif command.aggregate_type == "relation":
        _require_identity(command, ("relation_id",))
        _require_endpoints(command, ("source_entity_id", "target_entity_id"))
        _require_aggregate_identity(command, "relation_id")
    elif command.aggregate_type == "entity_mention":
        _require_identity(command, ("mention_id",))
        _require_endpoints(command, ("entity_id", "chunk_id"))
        _require_aggregate_identity(command, "mention_id")
    elif command.aggregate_type == "chunk_next":
        _require_identity(command, ("from_chunk_id", "to_chunk_id"))
        _require_endpoints(command, ("from_chunk_id", "to_chunk_id"))
        _require_aggregate_identity(command, "from_chunk_id")
        _require_endpoint_matches_identity(command, "from_chunk_id")
        _require_endpoint_matches_identity(command, "to_chunk_id")
    return command


def entity_upsert_command(
    *,
    entity_id: UUID | str,
    dataset_id: UUID | str,
    name: str,
    entity_type: str,
    description: str,
    importance_weight: float,
    generation: int,
) -> ProjectionCommand:
    """Build the frozen upsert command for one authoritative entity."""

    entity_id_text = str(entity_id)
    dataset_id_text = str(dataset_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity",
        operation="upsert",
        dataset_id=dataset_id_text,
        aggregate_id=entity_id_text,
        identity={"id": entity_id_text},
        endpoints={},
        properties={
            "id": entity_id_text,
            "dataset_id": dataset_id_text,
            "name": name,
            "entity_type": entity_type,
            "description": description,
            "importance_weight": importance_weight,
            "generation": generation,
        },
    )


def entity_delete_command(
    *,
    entity_id: UUID | str,
    dataset_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen delete command for one inactive authoritative entity."""

    entity_id_text = str(entity_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity",
        operation="delete",
        dataset_id=str(dataset_id),
        aggregate_id=entity_id_text,
        identity={"id": entity_id_text},
        endpoints={},
        properties={},
    )


def chunk_upsert_command(
    *,
    chunk_id: UUID | str,
    dataset_id: UUID | str,
    source_id: UUID | str,
    document_id: UUID | str,
    ordinal: int,
    generation: int,
) -> ProjectionCommand:
    """Build the frozen upsert command for one authoritative chunk."""

    chunk_id_text = str(chunk_id)
    dataset_id_text = str(dataset_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk",
        operation="upsert",
        dataset_id=dataset_id_text,
        aggregate_id=chunk_id_text,
        identity={"id": chunk_id_text},
        endpoints={},
        properties={
            "id": chunk_id_text,
            "dataset_id": dataset_id_text,
            "source_id": str(source_id),
            "document_id": str(document_id),
            "ordinal": ordinal,
            "generation": generation,
        },
    )


def chunk_delete_command(
    *,
    chunk_id: UUID | str,
    dataset_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen delete command for one inactive authoritative chunk."""

    chunk_id_text = str(chunk_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk",
        operation="delete",
        dataset_id=str(dataset_id),
        aggregate_id=chunk_id_text,
        identity={"id": chunk_id_text},
        endpoints={},
        properties={},
    )


def relation_upsert_command(
    *,
    relation_id: UUID | str,
    dataset_id: UUID | str,
    source_entity_id: UUID | str,
    target_entity_id: UUID | str,
    predicate: str,
    description: str,
    confidence: float,
    importance_weight: float,
    generation: int,
) -> ProjectionCommand:
    """Build the frozen upsert command for one authoritative relation."""

    relation_id_text = str(relation_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="relation",
        operation="upsert",
        dataset_id=str(dataset_id),
        aggregate_id=relation_id_text,
        identity={"relation_id": relation_id_text},
        endpoints={
            "source_entity_id": str(source_entity_id),
            "target_entity_id": str(target_entity_id),
        },
        properties={
            "relation_id": relation_id_text,
            "predicate": predicate,
            "description": description,
            "confidence": confidence,
            "importance_weight": importance_weight,
            "generation": generation,
        },
    )


def relation_delete_command(
    *,
    relation_id: UUID | str,
    dataset_id: UUID | str,
    source_entity_id: UUID | str,
    target_entity_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen delete command for one inactive authoritative relation."""

    relation_id_text = str(relation_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="relation",
        operation="delete",
        dataset_id=str(dataset_id),
        aggregate_id=relation_id_text,
        identity={"relation_id": relation_id_text},
        endpoints={
            "source_entity_id": str(source_entity_id),
            "target_entity_id": str(target_entity_id),
        },
        properties={},
    )


def entity_mention_upsert_command(
    *,
    mention_id: UUID | str,
    dataset_id: UUID | str,
    entity_id: UUID | str,
    chunk_id: UUID | str,
    confidence: float,
) -> ProjectionCommand:
    """Build the frozen upsert command for one persisted mention occurrence."""

    mention_id_text = str(mention_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity_mention",
        operation="upsert",
        dataset_id=str(dataset_id),
        aggregate_id=mention_id_text,
        identity={"mention_id": mention_id_text},
        endpoints={"entity_id": str(entity_id), "chunk_id": str(chunk_id)},
        properties={"mention_id": mention_id_text, "confidence": confidence},
    )


def entity_mention_delete_command(
    *,
    mention_id: UUID | str,
    dataset_id: UUID | str,
    entity_id: UUID | str,
    chunk_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen delete command for one non-authoritative mention edge."""

    mention_id_text = str(mention_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity_mention",
        operation="delete",
        dataset_id=str(dataset_id),
        aggregate_id=mention_id_text,
        identity={"mention_id": mention_id_text},
        endpoints={"entity_id": str(entity_id), "chunk_id": str(chunk_id)},
        properties={},
    )


def chunk_next_upsert_command(
    *,
    dataset_id: UUID | str,
    from_chunk_id: UUID | str,
    to_chunk_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen upsert command for one derived consecutive chunk edge."""

    from_chunk_id_text = str(from_chunk_id)
    to_chunk_id_text = str(to_chunk_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk_next",
        operation="upsert",
        dataset_id=str(dataset_id),
        aggregate_id=from_chunk_id_text,
        identity={"from_chunk_id": from_chunk_id_text, "to_chunk_id": to_chunk_id_text},
        endpoints={"from_chunk_id": from_chunk_id_text, "to_chunk_id": to_chunk_id_text},
        properties={},
    )


def chunk_next_delete_command(
    *,
    dataset_id: UUID | str,
    from_chunk_id: UUID | str,
    to_chunk_id: UUID | str,
) -> ProjectionCommand:
    """Build the frozen delete command for one derived consecutive chunk edge."""

    from_chunk_id_text = str(from_chunk_id)
    to_chunk_id_text = str(to_chunk_id)
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk_next",
        operation="delete",
        dataset_id=str(dataset_id),
        aggregate_id=from_chunk_id_text,
        identity={"from_chunk_id": from_chunk_id_text, "to_chunk_id": to_chunk_id_text},
        endpoints={"from_chunk_id": from_chunk_id_text, "to_chunk_id": to_chunk_id_text},
        properties={},
    )


def _schema_version(value: object) -> int:
    if type(value) is not int:
        raise ProjectionValidationError("projection schema_version must be integer 1")
    if value != GRAPH_PROJECTION_SCHEMA_VERSION:
        raise ProjectionValidationError("unsupported projection schema_version")
    return value


def _aggregate_type(value: object) -> ProjectionAggregateType:
    if not isinstance(value, str) or value not in ALLOWED_AGGREGATE_TYPES:
        raise ProjectionValidationError("unsupported projection aggregate_type")
    return cast(ProjectionAggregateType, value)


def _operation(value: object) -> ProjectionOperation:
    if not isinstance(value, str) or value not in ALLOWED_OPERATIONS:
        raise ProjectionValidationError("unsupported projection operation")
    return cast(ProjectionOperation, value)


def _string_value(value: object, field_name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise ProjectionValidationError(f"projection {field_name} must be a non-empty string")


def _string_mapping(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProjectionValidationError(f"projection {field_name} must be an object")
    return {str(key): _string_value(item, f"{field_name}.{key}") for key, item in value.items()}


def _properties(
    value: object,
    aggregate_type: ProjectionAggregateType,
    operation: ProjectionOperation,
) -> dict[str, ProjectionPropertyValue]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ProjectionValidationError("projection properties must be an object")
    if operation == "delete":
        if value:
            raise ProjectionValidationError("delete projection properties must be omitted or empty")
        return {}

    expected = _expected_properties(aggregate_type)
    actual = frozenset(str(key) for key in value)
    if actual != expected:
        raise ProjectionValidationError("projection properties do not match aggregate contract")
    return {str(key): _property_value(key, item) for key, item in value.items()}


def _property_value(key: object, value: object) -> ProjectionPropertyValue:
    key_text = str(key)
    if _is_identifier_property(key_text):
        return _string_value(value, f"properties.{key_text}")
    if value is None or isinstance(value, str | int | float):
        return value
    raise ProjectionValidationError(f"projection properties.{key_text} has unsupported value")


def _is_identifier_property(key: str) -> bool:
    return key == "id" or key.endswith("_id") or key in {"relation_id", "mention_id"}


def _expected_properties(aggregate_type: ProjectionAggregateType) -> frozenset[str]:
    if aggregate_type == "entity":
        return ENTITY_PROPERTIES
    if aggregate_type == "chunk":
        return CHUNK_PROPERTIES
    if aggregate_type == "relation":
        return RELATION_PROPERTIES
    if aggregate_type == "entity_mention":
        return ENTITY_MENTION_PROPERTIES
    if aggregate_type == "chunk_next":
        return CHUNK_NEXT_PROPERTIES
    raise ProjectionValidationError("unsupported projection aggregate_type")


def _require_identity(command: ProjectionCommand, expected_keys: tuple[str, ...]) -> None:
    if frozenset(command.identity) != frozenset(expected_keys):
        raise ProjectionValidationError("projection identity does not match aggregate contract")


def _require_endpoints(command: ProjectionCommand, expected_keys: tuple[str, ...]) -> None:
    if frozenset(command.endpoints) != frozenset(expected_keys):
        raise ProjectionValidationError("projection endpoints do not match aggregate contract")


def _require_no_endpoints(command: ProjectionCommand) -> None:
    if command.endpoints:
        raise ProjectionValidationError("node projection must not include endpoints")


def _require_aggregate_identity(command: ProjectionCommand, identity_key: str) -> None:
    if command.aggregate_id != command.identity[identity_key]:
        raise ProjectionValidationError("projection aggregate_id does not match identity")


def _require_endpoint_matches_identity(command: ProjectionCommand, key: str) -> None:
    if command.endpoints[key] != command.identity[key]:
        raise ProjectionValidationError("projection endpoint does not match identity")
