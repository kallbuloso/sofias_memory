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


def test_session_entry_routes_present_with_exact_methods_and_no_mutation() -> None:
    """SM-603: append-only SessionEntry API plus the lightweight Session
    query-history projection. No PATCH/DELETE surface of any kind, at the
    item or collection level, is ever introduced for SessionEntry."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert set(paths["/api/v1/sessions/{session_uuid}/entries"]) == {"get", "post"}
    assert set(paths["/api/v1/sessions/{session_uuid}/queries"]) == {"get"}
    assert "/api/v1/sessions/{session_uuid}/entries/{entry_id}" not in paths

    for path, operations in paths.items():
        if path.startswith("/api/v1/sessions/{session_uuid}/entries"):
            assert "patch" not in operations
            assert "delete" not in operations
            assert "put" not in operations


def test_recall_session_context_surface_is_present() -> None:
    """SM-604: `include_session_context` is a first-class, documented
    RecallRequest field (opt-in, default false) -- the SM-603 guard that
    asserted this surface did not exist yet is superseded by this positive
    contract."""

    schema = openapi_schema()
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    recall_request = schemas["RecallRequest"]
    assert isinstance(recall_request, dict)
    properties = recall_request["properties"]
    assert isinstance(properties, dict)

    include_session_context = properties["include_session_context"]
    assert include_session_context["default"] is False
    assert include_session_context["type"] == "boolean"
    assert "session_id" in properties

    recall_result = schemas["RecallResult"]
    assert isinstance(recall_result, dict)
    assert "session_uuid" in recall_result["properties"]

    query_provenance_result = schemas["QueryProvenanceResult"]
    assert isinstance(query_provenance_result, dict)
    assert "session_uuid" in query_provenance_result["properties"]
    assert "session_context" in query_provenance_result["properties"]
    assert "SessionContextProvenanceItem" in schemas


def test_remember_and_runs_session_uuid_surface_is_additive_only() -> None:
    """SM-605/SM-606: `session_uuid` on RememberTextResult/RunSummaryResult
    and the `session_uuid` filter on `GET /runs` are purely additive --
    nullable/optional, never a new requirement for a legacy caller."""

    schema = openapi_schema()
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    paths = schema["paths"]
    assert isinstance(paths, dict)

    remember_result = schemas["RememberTextResult"]
    assert isinstance(remember_result, dict)
    assert "session_uuid" in remember_result["properties"]
    assert "session_uuid" not in remember_result.get("required", [])

    run_summary = schemas["RunSummaryResult"]
    assert isinstance(run_summary, dict)
    assert "session_uuid" in run_summary["properties"]
    assert "session_uuid" not in run_summary.get("required", [])

    run_list_params = paths["/api/v1/runs"]["get"]["parameters"]
    session_uuid_params = [p for p in run_list_params if p["name"] == "session_uuid"]
    assert len(session_uuid_params) == 1
    assert session_uuid_params[0]["required"] is False

    # Public API filters by the structural session_uuid FK, never by the
    # textual session_id used on write endpoints.
    assert "session_id" not in {p["name"] for p in run_list_params}


def test_no_session_hard_delete_or_purge_surface_exists() -> None:
    """SM-606 SS 43: Sessions never gained a hard-delete/purge API, and
    Forget EVERYTHING is not, and never becomes, an alias for one."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert "delete" not in paths.get("/api/v1/sessions/{session_uuid}", {})
    assert "/api/v1/sessions/{session_uuid}/purge" not in paths
    assert "/api/v1/sessions/{session_uuid}/entries/{entry_id}" not in paths
    for path, operations in paths.items():
        if path.startswith("/api/v1/sessions"):
            assert "delete" not in operations, path


def test_skill_management_routes_present_with_exact_methods() -> None:
    """SM-702 scope: Skill management + immutable SkillRevision create/read,
    archive/restore, and current_revision rollback. SkillRevision itself
    gains no PATCH/DELETE (SM-701/SS 4.2: a revision is immutable once
    created) -- including the SM-703 import/export surface added below."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert set(paths["/api/v1/skills"]) == {"get", "post"}
    assert set(paths["/api/v1/skills/{skill_uuid}"]) == {"get", "patch"}
    assert "delete" not in paths["/api/v1/skills/{skill_uuid}"]
    assert set(paths["/api/v1/skills/{skill_uuid}/archive"]) == {"post"}
    assert set(paths["/api/v1/skills/{skill_uuid}/restore"]) == {"post"}
    assert set(paths["/api/v1/skills/{skill_uuid}/revisions"]) == {"get", "post"}
    assert set(paths["/api/v1/skills/{skill_uuid}/revisions/{revision}"]) == {"get"}

    for path, operations in paths.items():
        if path.startswith("/api/v1/skills/{skill_uuid}/revisions"):
            assert "patch" not in operations, path
            assert "delete" not in operations, path
            assert "put" not in operations, path


def test_skill_import_export_resolve_surface_present_no_extra_routes() -> None:
    """SM-703 scope: standalone SKILL.md import/export, exactly the three
    routes the Feature Contract froze -- no bundled package, no fourth
    import/export route. SM-704 scope: exactly one new operation,
    `POST /skills/resolve` -- no bundled/tool-execution surface added
    alongside it."""

    schema = openapi_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)

    assert set(paths["/api/v1/skills/import"]) == {"post"}
    assert set(paths["/api/v1/skills/{skill_uuid}/revisions/import"]) == {"post"}
    assert set(paths["/api/v1/skills/{skill_uuid}/revisions/{revision}/export"]) == {"get"}
    assert set(paths["/api/v1/skills/resolve"]) == {"post"}

    assert "/api/v1/skills/export" not in paths
    assert "/api/v1/skills/{skill_uuid}/export" not in paths
    assert "/api/v1/skills/{skill_uuid}/import" not in paths
    assert "/api/v1/skills/{skill_uuid}/resolve" not in paths


def test_skill_result_shape_never_exposes_procedure_or_internal_ids() -> None:
    schema = openapi_schema()
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)

    skill_result = schemas["SkillResult"]
    assert isinstance(skill_result, dict)
    skill_properties = skill_result["properties"]
    assert "procedure" not in skill_properties
    assert "resolution_embedding" not in skill_properties
    assert "content_sha256" not in skill_properties

    revision_list_item = schemas["SkillRevisionListItem"]
    assert isinstance(revision_list_item, dict)
    revision_list_properties = revision_list_item["properties"]
    assert "procedure" not in revision_list_properties
    assert "metadata" not in revision_list_properties

    revision_result = schemas["SkillRevisionResult"]
    assert isinstance(revision_result, dict)
    assert "procedure" in revision_result["properties"]
    assert "resolution_embedding" not in revision_result["properties"]

    skill_create_request = schemas["SkillCreateRequest"]
    assert isinstance(skill_create_request, dict)
    assert "procedure" in skill_create_request["properties"]

    skill_update_request = schemas["SkillUpdateRequest"]
    assert isinstance(skill_update_request, dict)
    assert set(skill_update_request["properties"]) == {"current_revision"}

    skill_resolve_match = schemas["SkillResolveMatch"]
    assert isinstance(skill_resolve_match, dict)
    resolve_properties = skill_resolve_match["properties"]
    assert set(resolve_properties) == {
        "skill_uuid",
        "name",
        "description",
        "current_revision",
        "tags",
        "declared_tools",
        "compatibility",
        "score",
    }
    assert "procedure" not in resolve_properties
    assert "metadata" not in resolve_properties
    assert "license" not in resolve_properties
    assert "content_sha256" not in resolve_properties
    assert "resolution_embedding" not in resolve_properties
    assert "archived_at" not in resolve_properties
    assert "created_at" not in resolve_properties
    assert "updated_at" not in resolve_properties


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
