from __future__ import annotations

from collections.abc import Mapping
from io import StringIO
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sofias_memory.app import create_app
from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.infrastructure.neo4j import (
    NEO4J_NOT_READY_DETAIL,
    NEO4J_SCHEMA_STATEMENTS,
    SHOW_CONSTRAINTS_QUERY,
    SHOW_INDEXES_QUERY,
    Neo4jCatalogObject,
    Neo4jReadinessChecker,
    Neo4jReadinessResult,
    Neo4jReadinessSnapshot,
    Neo4jResource,
    evaluate_neo4j_readiness,
)
from sofias_memory.infrastructure.postgres.readiness import (
    PostgresReadinessChecker,
    PostgresReadinessResult,
)
from sofias_memory.lifespan import NEO4J_STARTUP_PROBE_QUERY
from sofias_memory.observability.logging import clear_log_context, configure_logging

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"
VALID_NEO4J_DATABASE = "sofias-memory-readiness-test"
VALID_NEO4J_PASSWORD = "SUPER_SECRET_DO_NOT_LEAK_NEO4J_READINESS_PASSWORD"
VALID_LLM_API_KEY = "sk-fake-test-key"


class FakeRecord:
    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = data

    def data(self) -> Mapping[str, object]:
        return self._data


class FakeResult:
    def __init__(self, records: list[Mapping[str, object]] | None = None) -> None:
        self.records = [FakeRecord(record) for record in records or []]


class RecordingNeo4jDriver:
    def __init__(
        self,
        *,
        constraints: list[Mapping[str, object]] | None = None,
        indexes: list[Mapping[str, object]] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.constraints = constraints if constraints is not None else healthy_constraint_records()
        self.indexes = indexes if indexes is not None else healthy_index_records()
        self.failure = failure
        self.execute_query_calls: list[dict[str, object]] = []
        self.verify_connectivity_calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def verify_connectivity(self, **config: object) -> None:
        self.verify_connectivity_calls.append(config)
        raise AssertionError("Neo4j readiness must not call verify_connectivity")

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> FakeResult:
        if parameters_ is not None:
            raise AssertionError("Neo4j readiness does not use query parameters")
        self.execute_query_calls.append({"query": query_, "database_": database_})
        if self.failure is not None:
            raise self.failure
        if query_ == SHOW_CONSTRAINTS_QUERY:
            return FakeResult(self.constraints)
        if query_ == SHOW_INDEXES_QUERY:
            return FakeResult(self.indexes)
        if query_ == NEO4J_STARTUP_PROBE_QUERY:
            return FakeResult()
        if query_ in {statement.cypher for statement in NEO4J_SCHEMA_STATEMENTS}:
            return FakeResult()
        raise AssertionError(f"unexpected Neo4j query: {query_}")

    async def close(self) -> None:
        self.close_calls += 1


class FakeNeo4jResource:
    def __init__(self, driver: RecordingNeo4jDriver | None = None) -> None:
        self.driver = driver or RecordingNeo4jDriver()
        self.database = VALID_NEO4J_DATABASE
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await self.driver.close()


class FakeNeo4jReadinessChecker:
    def __init__(self, result: Neo4jReadinessResult | None = None) -> None:
        self.result = result or Neo4jReadinessResult(ready=True)
        self.check_calls = 0

    async def check(self) -> Neo4jReadinessResult:
        self.check_calls += 1
        return self.result


class ExplodingNeo4jReadinessChecker(FakeNeo4jReadinessChecker):
    async def check(self) -> Neo4jReadinessResult:
        self.check_calls += 1
        raise RuntimeError(f"neo4j exploded {VALID_NEO4J_PASSWORD}")


class FakePostgresReadinessChecker:
    def __init__(self, result: PostgresReadinessResult | None = None) -> None:
        self.result = result or PostgresReadinessResult(ready=True)
        self.check_calls = 0
        self.dispose_calls = 0

    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        return self.result

    async def dispose(self) -> None:
        self.dispose_calls += 1


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "neo4j_database": VALID_NEO4J_DATABASE,
        "llm_api_key": VALID_LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def as_neo4j_resource(fake_resource: FakeNeo4jResource) -> Neo4jResource:
    return cast(Neo4jResource, fake_resource)


def as_neo4j_checker(fake_checker: FakeNeo4jReadinessChecker) -> Neo4jReadinessChecker:
    return cast(Neo4jReadinessChecker, fake_checker)


def as_postgres_checker(fake_checker: FakePostgresReadinessChecker) -> PostgresReadinessChecker:
    return cast(PostgresReadinessChecker, fake_checker)


def catalog_object(
    name: str,
    *,
    object_type: str,
    label: str,
    property_name: str,
    entity_type: str = "NODE",
    state: str = "",
) -> Neo4jCatalogObject:
    return Neo4jCatalogObject(
        name=name,
        object_type=object_type,
        entity_type=entity_type,
        labels_or_types=frozenset({label}),
        properties=frozenset({property_name}),
        state=state,
    )


def healthy_snapshot() -> Neo4jReadinessSnapshot:
    return Neo4jReadinessSnapshot(
        constraints={
            "entity_id_unique": catalog_object(
                "entity_id_unique",
                object_type="UNIQUENESS",
                label="Entity",
                property_name="id",
            ),
            "chunk_id_unique": catalog_object(
                "chunk_id_unique",
                object_type="UNIQUENESS",
                label="Chunk",
                property_name="id",
            ),
        },
        indexes={
            "entity_dataset_id_index": catalog_object(
                "entity_dataset_id_index",
                object_type="RANGE",
                label="Entity",
                property_name="dataset_id",
                state="ONLINE",
            ),
            "chunk_dataset_id_index": catalog_object(
                "chunk_dataset_id_index",
                object_type="RANGE",
                label="Chunk",
                property_name="dataset_id",
                state="ONLINE",
            ),
            "entity_name_index": catalog_object(
                "entity_name_index",
                object_type="RANGE",
                label="Entity",
                property_name="name",
                state="ONLINE",
            ),
        },
    )


def healthy_constraint_records() -> list[Mapping[str, object]]:
    return [
        record("entity_id_unique", object_type="UNIQUENESS", label="Entity", property_name="id"),
        record("chunk_id_unique", object_type="UNIQUENESS", label="Chunk", property_name="id"),
    ]


def healthy_index_records() -> list[Mapping[str, object]]:
    return [
        record(
            "entity_dataset_id_index",
            object_type="RANGE",
            label="Entity",
            property_name="dataset_id",
            state="ONLINE",
        ),
        record(
            "chunk_dataset_id_index",
            object_type="RANGE",
            label="Chunk",
            property_name="dataset_id",
            state="ONLINE",
        ),
        record(
            "entity_name_index",
            object_type="RANGE",
            label="Entity",
            property_name="name",
            state="ONLINE",
        ),
    ]


def record(
    name: str,
    *,
    object_type: str,
    label: str,
    property_name: str,
    entity_type: str = "NODE",
    state: str = "",
) -> Mapping[str, object]:
    return {
        "name": name,
        "type": object_type,
        "state": state,
        "entityType": entity_type,
        "labelsOrTypes": [label],
        "properties": [property_name],
    }


def test_healthy_neo4j_readiness_snapshot_is_ready() -> None:
    result = evaluate_neo4j_readiness(healthy_snapshot())

    assert result.ready is True
    assert result.failures == ()


def test_show_indexes_query_reads_type_and_state() -> None:
    assert "YIELD name, state, type, entityType, labelsOrTypes, properties" in SHOW_INDEXES_QUERY
    assert "RETURN name, state, type, entityType, labelsOrTypes, properties" in SHOW_INDEXES_QUERY


def test_explicit_indexes_with_range_and_online_state_are_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(snapshot)

    assert result.ready is True
    for catalog_object in snapshot.indexes.values():
        assert catalog_object.object_type == "RANGE"
        assert catalog_object.state == "ONLINE"


def test_missing_entity_id_unique_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                name: value
                for name, value in snapshot.constraints.items()
                if name != "entity_id_unique"
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


def test_missing_chunk_id_unique_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                name: value
                for name, value in snapshot.constraints.items()
                if name != "chunk_id_unique"
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


@pytest.mark.parametrize(
    "missing_index",
    ["entity_dataset_id_index", "chunk_dataset_id_index", "entity_name_index"],
)
def test_missing_explicit_index_is_not_ready(missing_index: str) -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints=snapshot.constraints,
            indexes={
                name: value for name, value in snapshot.indexes.items() if name != missing_index
            },
        )
    )

    assert result.ready is False
    assert result.failures == ("indexes",)


def test_constraint_with_wrong_label_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                **snapshot.constraints,
                "entity_id_unique": catalog_object(
                    "entity_id_unique",
                    object_type="UNIQUENESS",
                    label="WrongLabel",
                    property_name="id",
                ),
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


def test_constraint_with_wrong_property_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                **snapshot.constraints,
                "chunk_id_unique": catalog_object(
                    "chunk_id_unique",
                    object_type="UNIQUENESS",
                    label="Chunk",
                    property_name="wrong_property",
                ),
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


def test_non_unique_constraint_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                **snapshot.constraints,
                "entity_id_unique": catalog_object(
                    "entity_id_unique",
                    object_type="RANGE",
                    label="Entity",
                    property_name="id",
                ),
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


def test_constraint_type_must_be_exact_uniqueness() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                **snapshot.constraints,
                "entity_id_unique": catalog_object(
                    "entity_id_unique",
                    object_type="NOT_UNIQUENESS",
                    label="Entity",
                    property_name="id",
                ),
            },
            indexes=snapshot.indexes,
        )
    )

    assert result.ready is False
    assert result.failures == ("constraints",)


def test_index_with_wrong_label_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints=snapshot.constraints,
            indexes={
                **snapshot.indexes,
                "entity_name_index": catalog_object(
                    "entity_name_index",
                    object_type="RANGE",
                    label="WrongLabel",
                    property_name="name",
                ),
            },
        )
    )

    assert result.ready is False
    assert result.failures == ("indexes",)


def test_index_with_wrong_property_is_not_ready() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints=snapshot.constraints,
            indexes={
                **snapshot.indexes,
                "chunk_dataset_id_index": catalog_object(
                    "chunk_dataset_id_index",
                    object_type="RANGE",
                    label="Chunk",
                    property_name="wrong_property",
                ),
            },
        )
    )

    assert result.ready is False
    assert result.failures == ("indexes",)


@pytest.mark.parametrize("index_type", ["TEXT", "FULLTEXT"])
def test_explicit_index_with_wrong_type_is_not_ready(index_type: str) -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints=snapshot.constraints,
            indexes={
                **snapshot.indexes,
                "entity_name_index": catalog_object(
                    "entity_name_index",
                    object_type=index_type,
                    label="Entity",
                    property_name="name",
                    state="ONLINE",
                ),
            },
        )
    )

    assert result.ready is False
    assert result.failures == ("indexes",)


@pytest.mark.parametrize("index_state", ["POPULATING", "FAILED"])
def test_explicit_index_without_online_state_is_not_ready(index_state: str) -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints=snapshot.constraints,
            indexes={
                **snapshot.indexes,
                "chunk_dataset_id_index": catalog_object(
                    "chunk_dataset_id_index",
                    object_type="RANGE",
                    label="Chunk",
                    property_name="dataset_id",
                    state=index_state,
                ),
            },
        )
    )

    assert result.ready is False
    assert result.failures == ("indexes",)


def test_extra_objects_and_backing_indexes_do_not_fail_readiness() -> None:
    snapshot = healthy_snapshot()

    result = evaluate_neo4j_readiness(
        Neo4jReadinessSnapshot(
            constraints={
                **snapshot.constraints,
                "future_constraint": catalog_object(
                    "future_constraint",
                    object_type="UNIQUENESS",
                    label="Future",
                    property_name="id",
                ),
            },
            indexes={
                **snapshot.indexes,
                "entity_id_unique_backing_index": catalog_object(
                    "entity_id_unique_backing_index",
                    object_type="RANGE",
                    label="Entity",
                    property_name="id",
                    state="ONLINE",
                ),
                "node_label_lookup": catalog_object(
                    "node_label_lookup",
                    object_type="LOOKUP",
                    label="",
                    property_name="",
                    state="ONLINE",
                ),
            },
        )
    )

    assert result.ready is True


@pytest.mark.asyncio
async def test_checker_queries_existing_resource_with_database_and_does_not_close() -> None:
    driver = RecordingNeo4jDriver()
    resource = FakeNeo4jResource(driver)
    checker = Neo4jReadinessChecker(as_neo4j_resource(resource))

    result = await checker.check()

    assert result.ready is True
    assert driver.execute_query_calls == [
        {"query": SHOW_CONSTRAINTS_QUERY, "database_": VALID_NEO4J_DATABASE},
        {"query": SHOW_INDEXES_QUERY, "database_": VALID_NEO4J_DATABASE},
    ]
    assert driver.verify_connectivity_calls == []
    assert resource.close_calls == 0
    assert driver.close_calls == 0


@pytest.mark.asyncio
async def test_checker_executes_only_read_only_catalog_queries() -> None:
    driver = RecordingNeo4jDriver()
    checker = Neo4jReadinessChecker(as_neo4j_resource(FakeNeo4jResource(driver)))

    await checker.check()

    queries = [str(call["query"]) for call in driver.execute_query_calls]
    assert queries == [SHOW_CONSTRAINTS_QUERY, SHOW_INDEXES_QUERY]
    for query in queries:
        normalized = query.upper()
        assert "CREATE" not in normalized
        assert "DROP" not in normalized
        assert "DELETE" not in normalized
        assert "MERGE" not in normalized
        assert " SET " not in normalized
        assert "REMOVE" not in normalized
        assert "APOC" not in normalized
        assert "GDS" not in normalized


@pytest.mark.asyncio
async def test_checker_does_not_call_schema_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_called(resource: object) -> None:
        raise AssertionError("readiness must not bootstrap Neo4j schema")

    monkeypatch.setattr(
        "sofias_memory.infrastructure.neo4j.schema.ensure_neo4j_schema", fail_if_called
    )
    checker = Neo4jReadinessChecker(as_neo4j_resource(FakeNeo4jResource()))

    result = await checker.check()

    assert result.ready is True


@pytest.mark.asyncio
async def test_checker_returns_not_ready_when_driver_fails() -> None:
    driver = RecordingNeo4jDriver(failure=RuntimeError(f"boom {VALID_NEO4J_PASSWORD}"))
    checker = Neo4jReadinessChecker(as_neo4j_resource(FakeNeo4jResource(driver)))

    result = await checker.check()

    assert result.ready is False
    assert result.failures == ("connection",)


@pytest.mark.asyncio
async def test_checker_returns_not_ready_when_database_is_inaccessible() -> None:
    driver = RecordingNeo4jDriver(failure=RuntimeError("database unavailable"))
    checker = Neo4jReadinessChecker(as_neo4j_resource(FakeNeo4jResource(driver)))

    result = await checker.check()

    assert result.ready is False
    assert result.failures == ("connection",)


@pytest.mark.asyncio
async def test_checker_logs_exception_type_without_secret(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = RecordingNeo4jDriver(failure=RuntimeError(f"boom {VALID_NEO4J_PASSWORD}"))
    checker = Neo4jReadinessChecker(as_neo4j_resource(FakeNeo4jResource(driver)))

    await checker.check()

    captured_text = f"{caplog.text}\n{capsys.readouterr().out}"
    assert "neo4j_readiness_check_failed" in captured_text
    assert "RuntimeError" in captured_text
    assert VALID_NEO4J_PASSWORD not in captured_text


def test_create_app_registers_neo4j_readiness_check() -> None:
    checker = FakeNeo4jReadinessChecker()
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=as_neo4j_resource(FakeNeo4jResource()),
        neo4j_readiness_checker=as_neo4j_checker(checker),
    )

    assert [registered.name for registered in app.state.readiness_checks] == ["neo4j"]


@pytest.mark.asyncio
async def test_ready_route_reports_postgres_and_neo4j_ready() -> None:
    postgres_checker = FakePostgresReadinessChecker(PostgresReadinessResult(ready=True))
    neo4j_checker = FakeNeo4jReadinessChecker(Neo4jReadinessResult(ready=True))
    app = create_app(
        make_settings(),
        postgres_readiness_checker=as_postgres_checker(postgres_checker),
        neo4j_resource=as_neo4j_resource(FakeNeo4jResource()),
        neo4j_readiness_checker=as_neo4j_checker(neo4j_checker),
    )

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response_json(response) == {
        "status": "ready",
        "checks": {
            "neo4j": {"ready": True},
            "postgres": {"ready": True},
        },
    }


@pytest.mark.asyncio
async def test_ready_route_reports_neo4j_not_ready_with_safe_detail() -> None:
    neo4j_checker = FakeNeo4jReadinessChecker(
        Neo4jReadinessResult(ready=False, failures=("constraints",))
    )
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=as_neo4j_resource(FakeNeo4jResource()),
        neo4j_readiness_checker=as_neo4j_checker(neo4j_checker),
    )

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response) == {
        "status": "not_ready",
        "checks": {"neo4j": {"ready": False, "detail": NEO4J_NOT_READY_DETAIL}},
    }


@pytest.mark.asyncio
async def test_ready_route_does_not_leak_neo4j_exception_details() -> None:
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=as_neo4j_resource(FakeNeo4jResource()),
        neo4j_readiness_checker=as_neo4j_checker(ExplodingNeo4jReadinessChecker()),
    )

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert VALID_NEO4J_PASSWORD not in response.text
    assert "neo4j exploded" not in response.text
    assert response_json(response)["checks"]["neo4j"]["detail"] == "check failed"


@pytest.mark.asyncio
async def test_live_route_does_not_call_neo4j() -> None:
    neo4j_checker = ExplodingNeo4jReadinessChecker()
    resource = FakeNeo4jResource(RecordingNeo4jDriver(failure=AssertionError("no live checks")))
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=as_neo4j_resource(resource),
        neo4j_readiness_checker=as_neo4j_checker(neo4j_checker),
    )

    async with make_client(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response_json(response) == {"status": "ok"}
    assert neo4j_checker.check_calls == 0
    assert resource.driver.execute_query_calls == []


def test_lifespan_probes_database_before_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    driver = RecordingNeo4jDriver()
    resource = FakeNeo4jResource(driver)

    async def bootstrap_spy(resource_to_bootstrap: object) -> None:
        assert resource_to_bootstrap is resource
        events.append("bootstrap")

    monkeypatch.setattr("sofias_memory.lifespan.ensure_neo4j_schema", bootstrap_spy)

    with TestClient(
        create_app(
            make_settings(),
            enable_postgres_readiness=False,
            neo4j_resource=as_neo4j_resource(resource),
        )
    ):
        pass

    assert driver.execute_query_calls[0] == {
        "query": NEO4J_STARTUP_PROBE_QUERY,
        "database_": VALID_NEO4J_DATABASE,
    }
    assert events == ["bootstrap"]


def test_lifespan_bootstraps_once_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_calls = 0
    resource = FakeNeo4jResource()

    async def bootstrap_spy(resource_to_bootstrap: object) -> None:
        nonlocal bootstrap_calls
        assert resource_to_bootstrap is resource
        bootstrap_calls += 1

    monkeypatch.setattr("sofias_memory.lifespan.ensure_neo4j_schema", bootstrap_spy)

    with TestClient(
        create_app(
            make_settings(),
            enable_postgres_readiness=False,
            neo4j_resource=as_neo4j_resource(resource),
        )
    ):
        pass

    assert bootstrap_calls == 1
    assert resource.close_calls == 1
    assert resource.driver.close_calls == 1


def test_lifespan_bootstrap_failure_prevents_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = FakeNeo4jResource()

    async def fail_bootstrap(resource_to_bootstrap: object) -> None:
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr("sofias_memory.lifespan.ensure_neo4j_schema", fail_bootstrap)

    with (
        pytest.raises(RuntimeError, match="bootstrap failed"),
        TestClient(
            create_app(
                make_settings(),
                enable_postgres_readiness=False,
                neo4j_resource=as_neo4j_resource(resource),
            )
        ),
    ):
        pass

    assert resource.close_calls == 1


def test_lifespan_bootstrap_logs_do_not_leak_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    clear_log_context()

    def configure_logging_for_test(log_level: str | int) -> None:
        configure_logging(log_level, stream=stream)

    async def fail_bootstrap(resource_to_bootstrap: object) -> None:
        raise RuntimeError(f"boom {VALID_NEO4J_PASSWORD}")

    monkeypatch.setattr("sofias_memory.lifespan.configure_logging", configure_logging_for_test)
    monkeypatch.setattr("sofias_memory.lifespan.ensure_neo4j_schema", fail_bootstrap)
    try:
        with (
            pytest.raises(RuntimeError),
            TestClient(
                create_app(
                    make_settings(),
                    enable_postgres_readiness=False,
                    neo4j_resource=as_neo4j_resource(FakeNeo4jResource()),
                )
            ),
        ):
            pass
    finally:
        clear_log_context()

    assert VALID_NEO4J_PASSWORD not in stream.getvalue()
