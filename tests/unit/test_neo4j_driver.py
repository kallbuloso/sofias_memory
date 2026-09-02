from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping
from io import StringIO
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.infrastructure.neo4j import Neo4jResource
from sofias_memory.infrastructure.neo4j.readiness import SHOW_CONSTRAINTS_QUERY, SHOW_INDEXES_QUERY
from sofias_memory.infrastructure.neo4j.schema import NEO4J_SCHEMA_STATEMENTS
from sofias_memory.lifespan import NEO4J_STARTUP_PROBE_QUERY
from sofias_memory.observability.logging import clear_log_context, configure_logging

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"
VALID_NEO4J_URI = "bolt://neo4j.example:7687"
VALID_NEO4J_USERNAME = "neo4j-test-user"
VALID_NEO4J_PASSWORD = "SUPER_SECRET_DO_NOT_LEAK_NEO4J_PASSWORD"
VALID_NEO4J_DATABASE = "sofias-memory-test"
VALID_LLM_API_KEY = "sk-fake-test-key"


class FakeAsyncNeo4jDriver:
    def __init__(self) -> None:
        self.verify_connectivity_calls: list[dict[str, object]] = []
        self.close_calls = 0
        self.query_calls = 0
        self.schema_calls = 0

    async def verify_connectivity(self, **config: object) -> None:
        if "database_" in config:
            raise AssertionError("verify_connectivity must receive session config key 'database'")
        self.verify_connectivity_calls.append(config)

    async def close(self) -> None:
        self.close_calls += 1

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> object:
        self.query_calls += 1
        raise AssertionError(
            f"unexpected query: {query_}; parameters={parameters_!r}; database={database_!r}"
        )

    async def install_schema(self) -> None:
        self.schema_calls += 1
        raise AssertionError("unexpected schema bootstrap")


class FakeRecord:
    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = data

    def data(self) -> Mapping[str, object]:
        return self._data


class FakeResult:
    def __init__(self, records: list[Mapping[str, object]] | None = None) -> None:
        self.records = [FakeRecord(record) for record in records or []]


class FakeLifecycleNeo4jDriver:
    def __init__(self) -> None:
        self.execute_query_calls: list[dict[str, object]] = []
        self.verify_connectivity_calls = 0
        self.close_calls = 0

    async def verify_connectivity(self, **config: object) -> None:
        self.verify_connectivity_calls += 1
        raise AssertionError("Neo4j connectivity should not be checked")

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> FakeResult:
        if parameters_ is not None:
            raise AssertionError("unexpected query parameters")
        self.execute_query_calls.append({"query": query_, "database_": database_})
        if query_ == NEO4J_STARTUP_PROBE_QUERY:
            return FakeResult()
        if query_ in {statement.cypher for statement in NEO4J_SCHEMA_STATEMENTS}:
            return FakeResult()
        if query_ == SHOW_CONSTRAINTS_QUERY:
            return FakeResult(
                [
                    {
                        "name": "entity_id_unique",
                        "type": "UNIQUENESS",
                        "entityType": "NODE",
                        "labelsOrTypes": ["Entity"],
                        "properties": ["id"],
                    },
                    {
                        "name": "chunk_id_unique",
                        "type": "UNIQUENESS",
                        "entityType": "NODE",
                        "labelsOrTypes": ["Chunk"],
                        "properties": ["id"],
                    },
                ]
            )
        if query_ == SHOW_INDEXES_QUERY:
            return FakeResult(
                [
                    {
                        "name": "entity_dataset_id_index",
                        "state": "ONLINE",
                        "type": "RANGE",
                        "entityType": "NODE",
                        "labelsOrTypes": ["Entity"],
                        "properties": ["dataset_id"],
                    },
                    {
                        "name": "chunk_dataset_id_index",
                        "state": "ONLINE",
                        "type": "RANGE",
                        "entityType": "NODE",
                        "labelsOrTypes": ["Chunk"],
                        "properties": ["dataset_id"],
                    },
                    {
                        "name": "entity_name_index",
                        "state": "ONLINE",
                        "type": "RANGE",
                        "entityType": "NODE",
                        "labelsOrTypes": ["Entity"],
                        "properties": ["name"],
                    },
                ]
            )
        raise AssertionError(f"unexpected query: {query_}")

    async def close(self) -> None:
        self.close_calls += 1


class FakeNeo4jResource:
    def __init__(self) -> None:
        self.driver = FakeLifecycleNeo4jDriver()
        self.database = VALID_NEO4J_DATABASE
        self.verify_connectivity_calls = 0
        self.close_calls = 0

    async def verify_connectivity(self) -> None:
        self.verify_connectivity_calls += 1
        raise AssertionError("Neo4j connectivity should not be checked")

    async def close(self) -> None:
        self.close_calls += 1
        await self.driver.close()


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_uri": VALID_NEO4J_URI,
        "neo4j_username": VALID_NEO4J_USERNAME,
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


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_import_neo4j_layer_does_not_create_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    import neo4j

    def fail_if_called(*args: object, **kwargs: object) -> FakeAsyncNeo4jDriver:
        raise AssertionError("Neo4j driver should not be created during import")

    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", fail_if_called)
    for module_name in list(sys.modules):
        if module_name.startswith("sofias_memory.infrastructure.neo4j"):
            sys.modules.pop(module_name)

    importlib.import_module("sofias_memory.infrastructure.neo4j")


def test_resource_factory_uses_explicit_settings() -> None:
    from sofias_memory.infrastructure.neo4j import create_neo4j_resource_from_settings

    captured_uri = ""
    captured_auth: tuple[str, str] | None = None
    fake_driver = FakeAsyncNeo4jDriver()

    def driver_factory(uri: str, auth: tuple[str, str]) -> FakeAsyncNeo4jDriver:
        nonlocal captured_uri, captured_auth
        captured_uri = uri
        captured_auth = auth
        return fake_driver

    settings = make_settings()

    resource = create_neo4j_resource_from_settings(settings, driver_factory=driver_factory)

    assert captured_uri == VALID_NEO4J_URI
    assert captured_auth == (VALID_NEO4J_USERNAME, VALID_NEO4J_PASSWORD)
    assert resource.driver is fake_driver


def test_resource_preserves_configured_database() -> None:
    resource = Neo4jResource(FakeAsyncNeo4jDriver(), database=VALID_NEO4J_DATABASE)

    assert resource.database == VALID_NEO4J_DATABASE


def test_resource_construction_does_not_connect_query_or_bootstrap_schema() -> None:
    fake_driver = FakeAsyncNeo4jDriver()

    Neo4jResource(fake_driver, database=VALID_NEO4J_DATABASE)

    assert fake_driver.verify_connectivity_calls == []
    assert fake_driver.query_calls == 0
    assert fake_driver.schema_calls == 0


@pytest.mark.asyncio
async def test_verify_connectivity_is_explicit_and_uses_database() -> None:
    fake_driver = FakeAsyncNeo4jDriver()
    resource = Neo4jResource(fake_driver, database=VALID_NEO4J_DATABASE)

    assert fake_driver.verify_connectivity_calls == []

    await resource.verify_connectivity()

    assert fake_driver.verify_connectivity_calls == [{"database": VALID_NEO4J_DATABASE}]


@pytest.mark.asyncio
async def test_close_is_async_and_closes_driver_once() -> None:
    fake_driver = FakeAsyncNeo4jDriver()
    resource = Neo4jResource(fake_driver, database=VALID_NEO4J_DATABASE)

    await resource.close()
    await resource.close()

    assert fake_driver.close_calls == 1


def test_resource_repr_and_str_do_not_leak_password_or_uri() -> None:
    settings = make_settings()
    fake_driver = FakeAsyncNeo4jDriver()

    resource = Neo4jResource(fake_driver, database=settings.neo4j_database)

    rendered = f"{resource!r} {resource!s}"
    assert VALID_NEO4J_PASSWORD not in rendered
    assert VALID_NEO4J_URI not in rendered
    assert "Neo4jResource" in rendered
    assert settings.neo4j_database in rendered


def test_create_app_stores_injected_neo4j_resource() -> None:
    from tests.unit._app_factory import create_app

    fake_resource = FakeNeo4jResource()
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=cast(Neo4jResource, fake_resource),
    )

    assert app.state.neo4j_resource is fake_resource


def test_create_app_can_disable_neo4j_resource() -> None:
    from tests.unit._app_factory import create_app

    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)

    assert not hasattr(app.state, "neo4j_resource")


def test_lifespan_closes_neo4j_resource_on_shutdown() -> None:
    from tests.unit._app_factory import create_app

    fake_resource = FakeNeo4jResource()
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_worker=False,
        neo4j_resource=cast(Neo4jResource, fake_resource),
    )

    with TestClient(app):
        pass

    assert fake_resource.close_calls == 1


def test_lifespan_logs_do_not_leak_neo4j_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit._app_factory import create_app

    stream = StringIO()

    def configure_logging_for_test(log_level: str | int) -> None:
        configure_logging(log_level, stream=stream)

    clear_log_context()
    monkeypatch.setattr("sofias_memory.lifespan.configure_logging", configure_logging_for_test)
    try:
        with TestClient(
            create_app(
                make_settings(),
                enable_postgres_readiness=False,
                enable_worker=False,
                neo4j_resource=cast(Neo4jResource, FakeNeo4jResource()),
            )
        ):
            pass
    finally:
        clear_log_context()

    output = stream.getvalue()
    records = read_log_records(stream)
    assert any(record["event"] == "application_starting" for record in records)
    assert any(record["event"] == "application_shutdown" for record in records)
    assert VALID_NEO4J_PASSWORD not in output
    assert VALID_NEO4J_URI not in output


@pytest.mark.asyncio
async def test_live_route_does_not_check_neo4j_connectivity() -> None:
    from tests.unit._app_factory import create_app

    fake_resource = FakeNeo4jResource()
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        neo4j_resource=cast(Neo4jResource, fake_resource),
    )

    async with make_client(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response_json(response) == {"status": "ok"}
    assert fake_resource.verify_connectivity_calls == 0


@pytest.mark.asyncio
async def test_readiness_reports_injected_neo4j_resource() -> None:
    from tests.unit._app_factory import create_app

    fake_resource = FakeNeo4jResource()
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_worker=False,
        neo4j_resource=cast(Neo4jResource, fake_resource),
    )

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response_json(response) == {
        "status": "ready",
        "checks": {"neo4j": {"ready": True}, "process_state": {"ready": True}},
    }
    assert fake_resource.verify_connectivity_calls == 0
