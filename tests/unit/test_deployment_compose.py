"""Deployment-critical invariants for ADR-0011's S3-storage rollout
(STORAGE-008) -- structural checks against the actual repository files, not
against a running Docker/Compose engine (unavailable in this environment;
see the STORAGE-008 deliverable for the disclosed limitation).

Plain text/line checks are used deliberately instead of a YAML parser: none
of `pyyaml`/an equivalent is a declared `[project.dependencies]` entry
(AGENTS.md SS9's "no new dependency without a concrete need"), and every
invariant below is expressible as a simple, robust string check against the
small, hand-written Compose files this repository actually maintains.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

COMPOSE_FILES = [
    REPO_ROOT / "compose.yaml",
    REPO_ROOT / "deploy" / "easypanel" / "compose.yaml",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# -- .env.example ------------------------------------------------------------


def test_env_example_default_storage_backend_is_filesystem() -> None:
    content = _read(REPO_ROOT / ".env.example")
    assert "STORAGE_BACKEND=filesystem" in content
    # Uncommented -- a real, active default value, not a commented example.
    assert "\nSTORAGE_BACKEND=filesystem\n" in content or content.startswith(
        "STORAGE_BACKEND=filesystem\n"
    )


def test_env_example_s3_fields_are_commented_optional_placeholders() -> None:
    content = _read(REPO_ROOT / ".env.example")
    for field in (
        "STORAGE_S3_BUCKET",
        "STORAGE_S3_PREFIX",
        "STORAGE_S3_REGION",
        "STORAGE_S3_ENDPOINT_URL",
        "STORAGE_S3_ACCESS_KEY_ID",
        "STORAGE_S3_SECRET_ACCESS_KEY",
        "STORAGE_S3_SESSION_TOKEN",
    ):
        assert f"# {field}=" in content, f"{field} must be a commented, optional placeholder"


def test_env_example_never_contains_a_real_looking_s3_secret() -> None:
    content = _read(REPO_ROOT / ".env.example")
    # AWS-shaped access key ids always start with this prefix; a real one
    # here would mean a leaked/hard-coded credential.
    assert "AKIA" not in content


# -- compose.yaml / deploy/easypanel/compose.yaml ----------------------------


def test_compose_files_default_storage_backend_is_filesystem_not_hardcoded_s3() -> None:
    for path in COMPOSE_FILES:
        content = _read(path)
        assert 'STORAGE_BACKEND: "${STORAGE_BACKEND:-filesystem}"' in content, path
        assert "STORAGE_BACKEND: s3" not in content, path
        assert 'STORAGE_BACKEND: "s3"' not in content, path


def test_compose_files_forward_s3_settings_without_hardcoded_secrets() -> None:
    for path in COMPOSE_FILES:
        content = _read(path)
        for field in (
            "STORAGE_S3_BUCKET",
            "STORAGE_S3_PREFIX",
            "STORAGE_S3_REGION",
            "STORAGE_S3_ENDPOINT_URL",
            "STORAGE_S3_ACCESS_KEY_ID",
            "STORAGE_S3_SECRET_ACCESS_KEY",
            "STORAGE_S3_SESSION_TOKEN",
            "STORAGE_S3_MAX_CONCURRENCY",
        ):
            assert f'{field}: "${{{field}:-' in content, f"{field} missing from {path}"
        assert "AKIA" not in content


def test_compose_files_data_directory_volume_remains_mandatory() -> None:
    for path in COMPOSE_FILES:
        content = _read(path)
        assert 'DATA_DIRECTORY: "/data/sources"' in content, path
        assert "sofias_memory_sources:/data/sources" in content, path
        assert "sofias_memory_sources:" in content.split("volumes:")[-1], path


def test_compose_files_healthcheck_targets_health_live_not_ready() -> None:
    for path in COMPOSE_FILES:
        content = _read(path)
        # The sofias-memory service's own top-level block -- isolate it from
        # the postgres/neo4j service blocks below it (each starts at column
        # 2, unlike "postgres:" appearing inside sofias-memory's own
        # `depends_on:` at column 6).
        service_section = content.split("\n  postgres:\n")[0]
        assert "/health/live" in service_section, path
        assert "/health/ready" not in service_section, path


def test_compose_files_never_auto_run_alembic() -> None:
    """`alembic upgrade head` may appear only in a comment (documenting the
    explicit, operator-run procedure, AGENTS.md SS14) -- never as part of an
    actual executed `command:`/`entrypoint:`/healthcheck instruction."""

    for path in COMPOSE_FILES:
        content = _read(path)
        for line in content.splitlines():
            if "alembic upgrade head" in line:
                assert line.strip().startswith("#"), f"{path}: {line!r}"


def test_compose_files_declare_exactly_one_replica_where_specified() -> None:
    """No `deploy:`/`replicas:` stanza exists in either file today (plain
    `docker compose`, not Swarm) -- if one is ever added, it must not exceed
    a single replica for the `sofias-memory` service (ADR-0011 D43)."""

    for path in COMPOSE_FILES:
        content = _read(path)
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("replicas:"):
                assert stripped == "replicas: 1", f"{path}: {stripped}"


# -- docs/operations.md -------------------------------------------------------


def test_operations_doc_upgrade_stops_the_container_before_starting_it() -> None:
    content = _read(REPO_ROOT / "docs" / "operations.md")
    upgrade_section = content.split("## 4. Upgrade")[1].split("## 5. Rollback")[0]
    stop_index = upgrade_section.index("docker compose stop sofias-memory")
    start_index = upgrade_section.index("docker compose up -d")
    assert stop_index < start_index
    # Confirm it is genuinely the sofias-memory service being started here
    # (not truncated mid-command), tolerant of the doc's own line wrapping.
    assert "sofias-memory" in upgrade_section[start_index : start_index + 40]


def test_operations_doc_never_recommends_destructive_volume_cleanup_for_upgrade() -> None:
    content = _read(REPO_ROOT / "docs" / "operations.md")
    assert "rm -rf" not in content
    assert "volume rm" not in content
    assert "volume prune" not in content
    # The one `docker compose down -v` mention is a disposable test-drill
    # destruction step, never presented as normal upgrade/deploy guidance.
    down_v_occurrences = content.count("down -v")
    assert down_v_occurrences == 1
    surrounding = content[content.index("down -v") - 200 : content.index("down -v") + 50]
    assert "Destruction proof" in surrounding


def test_operations_doc_documents_the_full_storage_env_surface() -> None:
    content = _read(REPO_ROOT / "docs" / "operations.md")
    section = content.split("## 13. S3-compatible Source storage")[1]
    for field in (
        "STORAGE_BACKEND",
        "STORAGE_S3_BUCKET",
        "STORAGE_S3_PREFIX",
        "STORAGE_S3_REGION",
        "STORAGE_S3_ENDPOINT_URL",
        "STORAGE_S3_ACCESS_KEY_ID",
        "STORAGE_S3_SECRET_ACCESS_KEY",
        "STORAGE_S3_SESSION_TOKEN",
        "STORAGE_S3_MAX_CONCURRENCY",
    ):
        assert field in section, field
