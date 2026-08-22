"""OpenAPI-surface contract checks for the SM-508 Runs read API."""

from __future__ import annotations

from typing import cast

from sofias_memory.app import create_app
from sofias_memory.config import Settings

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

EXPECTED_RUNS_PATHS = {
    "/api/v1/runs",
    "/api/v1/runs/{run_id}",
}

FORBIDDEN_RUNS_PATHS = {
    "/api/v1/runs/{run_id}/retry",
    "/api/v1/runs/{run_id}/cancel",
}


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


def openapi_schemas() -> dict[str, dict[str, object]]:
    app = create_app(make_settings())
    return cast(dict[str, dict[str, object]], app.openapi()["components"]["schemas"])


def test_both_sm_508_run_routes_exist() -> None:
    paths = openapi_paths()
    assert EXPECTED_RUNS_PATHS.issubset(paths)


def test_no_extra_run_routes_exist_this_story() -> None:
    paths = openapi_paths()
    runs_paths = {path for path in paths if path.startswith("/api/v1/runs")}
    assert runs_paths == EXPECTED_RUNS_PATHS


def test_sm_514_retry_and_cancel_routes_are_not_registered() -> None:
    paths = openapi_paths()
    assert FORBIDDEN_RUNS_PATHS.isdisjoint(paths)


def test_run_routes_only_expose_get() -> None:
    paths = openapi_paths()
    for path, operations in paths.items():
        if path.startswith("/api/v1/runs"):
            assert set(operations.keys()) <= {"get"}, f"non-read method on {path}: {operations}"


def test_run_list_route_declares_expected_filters() -> None:
    paths = openapi_paths()
    parameters = cast(list[dict[str, object]], paths["/api/v1/runs"]["get"]["parameters"])
    names = {cast(str, parameter["name"]) for parameter in parameters}
    assert {"limit", "offset", "status", "type", "dataset_id"}.issubset(names)


def test_run_step_error_references_the_structured_schema_not_a_free_form_dict() -> None:
    schemas = openapi_schemas()
    step_error_property = cast(dict[str, object], schemas["RunStepResult"]["properties"]["error"])
    any_of = cast(list[dict[str, object]], step_error_property["anyOf"])
    referenced = {entry.get("$ref") for entry in any_of if "$ref" in entry}

    assert referenced == {"#/components/schemas/RunStepErrorResult"}


def test_run_step_error_result_schema_only_declares_code_and_message() -> None:
    schemas = openapi_schemas()
    error_schema = schemas["RunStepErrorResult"]

    assert set(cast(dict[str, object], error_schema["properties"]).keys()) == {"code", "message"}
    assert error_schema.get("additionalProperties") is False
