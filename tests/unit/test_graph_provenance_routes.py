"""OpenAPI-surface contract checks for the SM-424 graph/provenance routes."""

from __future__ import annotations

from typing import cast

from sofias_memory.app import create_app
from sofias_memory.config import Settings

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

EXPECTED_GRAPH_PROVENANCE_PATHS = {
    "/api/v1/graph/schema",
    "/api/v1/graph/subgraph",
    "/api/v1/graph/path",
    "/api/v1/provenance/source/{source_id}",
    "/api/v1/provenance/relation/{relation_id}",
    "/api/v1/provenance/query/{query_id}",
}

FORBIDDEN_PREFIXES = (
    "/auth",
    "/users",
    "/permissions",
    "/api-keys",
    "/settings",
    "/configuration",
    "/sync",
    "/cloud",
    "/serve",
    "/push",
    "/slack",
    "/integrations",
    "/agents",
    "/graph/cypher",
    "/graph/query",
    "/graph/entities",
    "/graph/write",
    "/provenance/chunk",
    "/provenance/document",
)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def openapi_paths() -> dict[str, dict[str, object]]:
    app = create_app(make_settings())
    return cast(dict[str, dict[str, object]], app.openapi()["paths"])


def test_all_six_frozen_graph_provenance_routes_exist() -> None:
    paths = openapi_paths()
    assert EXPECTED_GRAPH_PROVENANCE_PATHS.issubset(paths)


def test_no_raw_cypher_or_extra_graph_routes_exist() -> None:
    paths = openapi_paths()
    graph_paths = {path for path in paths if path.startswith("/api/v1/graph")}
    assert graph_paths == {"/api/v1/graph/schema", "/api/v1/graph/subgraph", "/api/v1/graph/path"}


def test_no_extra_provenance_routes_exist() -> None:
    paths = openapi_paths()
    provenance_paths = {path for path in paths if path.startswith("/api/v1/provenance")}
    assert provenance_paths == {
        "/api/v1/provenance/source/{source_id}",
        "/api/v1/provenance/relation/{relation_id}",
        "/api/v1/provenance/query/{query_id}",
    }


def test_no_forbidden_prefixes_are_registered() -> None:
    paths = openapi_paths()
    for path in paths:
        for prefix in FORBIDDEN_PREFIXES:
            assert not path.startswith(prefix), f"forbidden route registered: {path}"


def test_graph_and_provenance_routes_only_expose_get() -> None:
    paths = openapi_paths()
    for path, operations in paths.items():
        if path.startswith("/api/v1/graph") or path.startswith("/api/v1/provenance"):
            assert set(operations.keys()) <= {"get"}, f"non-read method on {path}: {operations}"
