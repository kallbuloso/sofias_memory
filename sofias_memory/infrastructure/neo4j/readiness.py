"""Read-only Neo4j readiness checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sofias_memory.infrastructure.neo4j.driver import Neo4jResource
from sofias_memory.infrastructure.neo4j.schema import NEO4J_SCHEMA_STATEMENTS
from sofias_memory.observability.logging import get_logger

NEO4J_NOT_READY_DETAIL = "neo4j not ready"

SHOW_CONSTRAINTS_QUERY = " ".join(
    (
        "SHOW CONSTRAINTS",
        "YIELD name, type, entityType, labelsOrTypes, properties",
        "RETURN name, type, entityType, labelsOrTypes, properties",
    )
)
SHOW_INDEXES_QUERY = " ".join(
    (
        "SHOW INDEXES",
        "YIELD name, state, type, entityType, labelsOrTypes, properties",
        "RETURN name, state, type, entityType, labelsOrTypes, properties",
    )
)

NEO4J_ONLINE_INDEX_STATE = "ONLINE"
NEO4J_RANGE_INDEX_TYPE = "RANGE"
NEO4J_UNIQUE_CONSTRAINT_TYPE = "UNIQUENESS"


@dataclass(frozen=True)
class Neo4jCatalogObject:
    """Structured Neo4j schema catalog object used by readiness."""

    name: str
    object_type: str
    entity_type: str
    labels_or_types: frozenset[str]
    properties: frozenset[str]
    state: str = ""


@dataclass(frozen=True)
class Neo4jReadinessSnapshot:
    """Neo4j schema state needed by the readiness evaluator."""

    constraints: Mapping[str, Neo4jCatalogObject]
    indexes: Mapping[str, Neo4jCatalogObject]


@dataclass(frozen=True)
class Neo4jReadinessResult:
    """Internal readiness result; failures are intentionally not public details."""

    ready: bool
    failures: tuple[str, ...] = ()


class Neo4jReadinessChecker:
    """Read-only Neo4j readiness checker for one application resource."""

    def __init__(self, resource: Neo4jResource) -> None:
        self._resource = resource

    async def check(self) -> Neo4jReadinessResult:
        try:
            constraints_result = await self._resource.driver.execute_query(
                SHOW_CONSTRAINTS_QUERY,
                database_=self._resource.database,
            )
            indexes_result = await self._resource.driver.execute_query(
                SHOW_INDEXES_QUERY,
                database_=self._resource.database,
            )
        except Exception as exc:
            _log_neo4j_readiness_failure(type(exc).__name__)
            return Neo4jReadinessResult(ready=False, failures=("connection",))

        snapshot = Neo4jReadinessSnapshot(
            constraints=catalog_objects_by_name(result_records(constraints_result)),
            indexes=catalog_objects_by_name(result_records(indexes_result)),
        )
        return evaluate_neo4j_readiness(snapshot)


def evaluate_neo4j_readiness(snapshot: Neo4jReadinessSnapshot) -> Neo4jReadinessResult:
    failures: list[str] = []

    if not _required_constraints_are_ready(snapshot.constraints):
        failures.append("constraints")
    if not _required_indexes_are_ready(snapshot.indexes):
        failures.append("indexes")

    return Neo4jReadinessResult(ready=not failures, failures=tuple(failures))


def catalog_objects_by_name(
    records: Iterable[Mapping[str, object]],
) -> dict[str, Neo4jCatalogObject]:
    objects: dict[str, Neo4jCatalogObject] = {}
    for record in records:
        catalog_object = catalog_object_from_record(record)
        objects[catalog_object.name] = catalog_object
    return objects


def catalog_object_from_record(record: Mapping[str, object]) -> Neo4jCatalogObject:
    return Neo4jCatalogObject(
        name=str(record.get("name", "")),
        object_type=str(record.get("type", "")),
        entity_type=str(record.get("entityType", "")),
        labels_or_types=object_set(record.get("labelsOrTypes")),
        properties=object_set(record.get("properties")),
        state=str(record.get("state", "")),
    )


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record_data(record) for record in records]


def record_data(record: object) -> Mapping[str, object]:
    if isinstance(record, Mapping):
        return record
    data_method = getattr(record, "data", None)
    if callable(data_method):
        data = data_method()
        if isinstance(data, Mapping):
            return data
    raise TypeError("Neo4j record does not expose mapping data")


def object_set(value: object) -> frozenset[str]:
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(str(item) for item in value)
    return frozenset()


def _required_constraints_are_ready(
    constraints: Mapping[str, Neo4jCatalogObject],
) -> bool:
    for statement in NEO4J_SCHEMA_STATEMENTS:
        if statement.kind != "constraint":
            continue
        catalog_object = constraints.get(statement.name)
        if catalog_object is None:
            return False
        if not _catalog_object_targets_statement(
            catalog_object, statement.label, statement.property_name
        ):
            return False
        if catalog_object.object_type.upper() != NEO4J_UNIQUE_CONSTRAINT_TYPE:
            return False
    return True


def _required_indexes_are_ready(indexes: Mapping[str, Neo4jCatalogObject]) -> bool:
    for statement in NEO4J_SCHEMA_STATEMENTS:
        if statement.kind != "index":
            continue
        catalog_object = indexes.get(statement.name)
        if catalog_object is None:
            return False
        if not _catalog_object_targets_statement(
            catalog_object, statement.label, statement.property_name
        ):
            return False
        if catalog_object.object_type.upper() != NEO4J_RANGE_INDEX_TYPE:
            return False
        if catalog_object.state.upper() != NEO4J_ONLINE_INDEX_STATE:
            return False
    return True


def _catalog_object_targets_statement(
    catalog_object: Neo4jCatalogObject,
    label: str,
    property_name: str,
) -> bool:
    return (
        catalog_object.entity_type.upper() == "NODE"
        and label in catalog_object.labels_or_types
        and property_name in catalog_object.properties
    )


def _log_neo4j_readiness_failure(exception_type: str) -> None:
    get_logger(__name__).warning(
        "neo4j_readiness_check_failed",
        exception_type=exception_type,
    )
