"""OpenAPI-surface contract checks for administrative Dataset delete
(SM-515, ADR-0010)."""

from __future__ import annotations

from typing import cast

from sofias_memory.app import create_app
from sofias_memory.config import Settings

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


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


def test_delete_dataset_route_exists() -> None:
    paths = openapi_paths()
    assert "/api/v1/datasets/{dataset_id}" in paths
    assert "delete" in paths["/api/v1/datasets/{dataset_id}"]


def test_delete_dataset_route_declares_no_request_body() -> None:
    paths = openapi_paths()
    assert "requestBody" not in paths["/api/v1/datasets/{dataset_id}"]["delete"]


def test_forbidden_route_prefixes_still_absent_with_dataset_delete() -> None:
    """ADR-0010/AGENTS.md 12: SM-515 must not introduce any of the
    permanently-forbidden route prefixes."""

    paths = openapi_paths()
    forbidden_prefixes = (
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
        "/skills",
        "/proposals",
    )
    for path in paths:
        for prefix in forbidden_prefixes:
            assert not path.startswith(f"/api/v1{prefix}"), path
