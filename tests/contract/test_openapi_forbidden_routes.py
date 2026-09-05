"""OpenAPI contract audit (SM-516 SS 45-46, AGENTS.md SS 12).

Fails if a prohibited route surface, an exposed secret default/example, or a
provider/DB/Neo4j configuration schema ever appears in the generated
OpenAPI document -- the one place a new route or a leaked example value
would be caught before it ships.
"""

from __future__ import annotations

import json

from sofias_memory.app import create_app
from sofias_memory.config import Settings

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake-db-password@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

FORBIDDEN_PATH_PREFIXES = (
    "/auth",
    "/login",
    "/register",
    "/users",
    "/roles",
    "/permissions",
    "/settings",
    "/configuration",
    "/sync",
    "/cypher",
    "/debug",
    "/metrics",
    "/api-keys",
    "/cloud",
    "/serve",
    "/push",
    "/slack",
    "/integrations",
    "/agents",
    "/skills",
    "/proposals",
    "/api/v1/metrics",
    "/debug/metrics",
    "/admin/metrics",
)

EXPECTED_RUN_ROUTES = (
    ("GET", "/api/v1/runs"),
    ("GET", "/api/v1/runs/{run_id}"),
    ("POST", "/api/v1/runs/{run_id}/retry"),
    ("POST", "/api/v1/runs/{run_id}/cancel"),
)

EXPECTED_HEALTH_ROUTES = (
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
)

SECRET_VALUES = (EXPECTED_API_KEY, DATABASE_URL, NEO4J_PASSWORD, LLM_API_KEY, "fake-db-password")


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_name="Sofias Memory Test",
        app_version="9.8.7",
        app_env="test",
    )


def openapi_schema() -> dict[str, object]:
    app = create_app(make_settings())
    return app.openapi()


def test_no_forbidden_route_prefix_appears() -> None:
    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    offenders = [
        path
        for path in paths
        if any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in FORBIDDEN_PATH_PREFIXES
        )
        or path in FORBIDDEN_PATH_PREFIXES
    ]
    assert offenders == []


def test_health_routes_present_and_exempt_from_api_key() -> None:
    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    for method, path in EXPECTED_HEALTH_ROUTES:
        assert path in paths, f"missing expected health route: {path}"
        operation = paths[path][method.lower()]
        security = operation.get("security")
        assert security in (None, []), f"{method} {path} must not require X-API-Key"


def test_runs_routes_present() -> None:
    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    for method, path in EXPECTED_RUN_ROUTES:
        assert path in paths, f"missing expected run route: {path}"
        assert method.lower() in paths[path], f"missing {method} on {path}"


def test_dataset_delete_route_present() -> None:
    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert "/api/v1/datasets/{dataset_id}" in paths
    assert "delete" in paths["/api/v1/datasets/{dataset_id}"]


def test_session_management_routes_present_with_exact_methods() -> None:
    """SM-602 scope: only Session management, no hard delete."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert set(paths["/api/v1/sessions"]) == {"get", "post"}
    assert set(paths["/api/v1/sessions/{session_uuid}"]) == {"get", "patch"}
    assert "delete" not in paths["/api/v1/sessions/{session_uuid}"]
    assert set(paths["/api/v1/sessions/{session_uuid}/archive"]) == {"post"}
    assert set(paths["/api/v1/sessions/{session_uuid}/restore"]) == {"post"}


def test_session_entry_and_recall_context_surfaces_not_yet_introduced() -> None:
    """SM-603/SM-604 scope, explicitly deferred by SM-602."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert "/api/v1/sessions/{session_uuid}/entries" not in paths
    assert "/api/v1/sessions/{session_uuid}/queries" not in paths
    assert "include_session_context" not in json.dumps(schema)


def test_private_routes_require_api_key_security() -> None:
    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    exempt = {path for _method, path in EXPECTED_HEALTH_ROUTES}
    for path, operations in paths.items():
        if path in exempt:
            continue
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert operation.get("security"), (
                f"{method.upper()} {path} is missing X-API-Key security"
            )


def test_no_secret_value_appears_anywhere_in_schema() -> None:
    serialized = json.dumps(openapi_schema())
    for secret in SECRET_VALUES:
        assert secret not in serialized, f"secret value leaked into OpenAPI schema: {secret!r}"


def test_no_provider_or_database_configuration_schema_exposed() -> None:
    schema = openapi_schema()
    components = schema.get("components", {})
    assert isinstance(components, dict)
    component_schemas = components.get("schemas", {})
    assert isinstance(component_schemas, dict)
    forbidden_schema_name_fragments = (
        "DatabaseUrl",
        "Neo4jPassword",
        "LlmApiKey",
        "EmbeddingApiKey",
        "ProviderConfig",
        "Settings",
    )
    for name in component_schemas:
        assert not any(fragment in name for fragment in forbidden_schema_name_fragments), (
            f"provider/DB configuration schema leaked into OpenAPI: {name}"
        )
