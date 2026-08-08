from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sofias_memory.config import (
    API_KEY_PREFIX,
    FINGERPRINT_SCHEMA_VERSION,
    Settings,
    build_config_fingerprint,
    build_config_fingerprint_payload,
    canonical_config_fingerprint_payload,
    load_settings,
)

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_LLM_API_KEY = "sk-fake-test-key"
VALID_NEO4J_PASSWORD = "fake-neo4j-password"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"

SETTINGS_ENV_NAMES = {
    "APP_NAME",
    "APP_ENV",
    "APP_VERSION",
    "API_KEY",
    "HTTP_HOST",
    "HTTP_PORT",
    "LOG_LEVEL",
    "CORS_ALLOWED_ORIGINS",
    "MAX_REQUEST_BODY_MB",
    "REQUEST_WAIT_TIMEOUT_SECONDS",
    "DATABASE_URL",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "DATA_DIRECTORY",
    "TEMP_DIRECTORY",
    "MAX_SOURCE_SIZE_MB",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "LLM_MAX_CONCURRENCY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_MAX_CONCURRENCY",
    "CHUNK_MAX_TOKENS",
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_MIN_TOKENS",
    "RECALL_VECTOR_TOP_K",
    "RECALL_LEXICAL_TOP_K",
    "RECALL_GRAPH_SEED_TOP_K",
    "RECALL_GRAPH_DEPTH",
    "RECALL_GRAPH_MAX_NODES",
    "RECALL_DEFAULT_TOP_K",
    "RECALL_MAX_TOP_K",
    "RECALL_RRF_K",
    "WORKER_ENABLED",
    "WORKER_POLL_INTERVAL_MS",
    "WORKER_STALE_AFTER_SECONDS",
    "WORKER_MAX_CONCURRENT_DATASETS",
    "WORKER_MAX_CONCURRENT_READS",
    "STORE_QUERY_CONTENT",
    "LOG_DOCUMENT_CONTENT",
    "LOG_LLM_PAYLOADS",
}


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTINGS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def minimal_settings_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "llm_api_key": VALID_LLM_API_KEY,
    }
    values.update(overrides)
    return values


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **minimal_settings_values(**overrides))


def assert_invalid(**overrides: Any) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_minimal_valid_configuration_uses_prd_defaults() -> None:
    settings = make_settings()

    assert settings.app_name == "Sofias Memory"
    assert settings.http_port == 8000
    assert settings.embedding_dimensions == 3072
    assert settings.chunk_max_tokens == 900
    assert settings.recall_default_top_k == 12
    assert settings.worker_enabled is True
    assert settings.log_document_content is False


def test_api_key_valid() -> None:
    settings = make_settings(api_key=f"{API_KEY_PREFIX}{'b' * 32}")

    assert settings.api_key.get_secret_value().startswith(API_KEY_PREFIX)


def test_api_key_without_prefix_is_rejected() -> None:
    assert_invalid(api_key="not-sf-prefixed-value-with-enough-characters")


def test_api_key_too_short_is_rejected() -> None:
    assert_invalid(api_key=f"{API_KEY_PREFIX}{'a' * 31}")


def test_api_key_with_non_url_safe_characters_is_rejected() -> None:
    assert_invalid(api_key=f"{API_KEY_PREFIX}{'a' * 31}!")


def test_api_key_missing_is_rejected() -> None:
    values = minimal_settings_values()
    del values["api_key"]

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_api_key_placeholder_is_rejected() -> None:
    assert_invalid(api_key="sf-change-me-with-at-least-32-random-characters")


def test_api_key_whitespace_only_is_rejected() -> None:
    assert_invalid(api_key=" " * 40)


def test_llm_api_key_missing_is_rejected() -> None:
    values = minimal_settings_values()
    del values["llm_api_key"]

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "field_name",
    [
        "database_url",
        "neo4j_password",
        "llm_api_key",
    ],
)
def test_required_secrets_reject_whitespace_only(field_name: str) -> None:
    assert_invalid(**{field_name: "   "})


def test_embedding_api_key_inherits_llm_api_key_when_missing() -> None:
    settings = make_settings()

    assert settings.embedding_api_key is not None
    assert settings.embedding_api_key.get_secret_value() == VALID_LLM_API_KEY


def test_embedding_api_key_inherits_llm_api_key_when_empty() -> None:
    settings = make_settings(embedding_api_key="")

    assert settings.embedding_api_key is not None
    assert settings.embedding_api_key.get_secret_value() == VALID_LLM_API_KEY


def test_embedding_api_key_inherits_llm_api_key_when_whitespace_only() -> None:
    settings = make_settings(embedding_api_key="   ")

    assert settings.embedding_api_key is not None
    assert settings.embedding_api_key.get_secret_value() == VALID_LLM_API_KEY


def test_embedding_api_key_can_override_llm_api_key() -> None:
    settings = make_settings(embedding_api_key="sk-fake-embedding-key")

    assert settings.embedding_api_key is not None
    assert settings.embedding_api_key.get_secret_value() == "sk-fake-embedding-key"


def test_embedding_dimensions_invalid() -> None:
    assert_invalid(embedding_dimensions=0)


def test_chunk_overlap_greater_than_or_equal_to_max_is_rejected() -> None:
    assert_invalid(chunk_max_tokens=100, chunk_overlap_tokens=100)


def test_chunk_min_greater_than_max_is_rejected() -> None:
    assert_invalid(chunk_max_tokens=100, chunk_min_tokens=101)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("http_port", 0),
        ("max_request_body_mb", 0),
        ("request_wait_timeout_seconds", 0),
        ("database_pool_size", 0),
        ("database_max_overflow", -1),
        ("max_source_size_mb", 0),
        ("llm_timeout_seconds", 0),
        ("llm_max_retries", -1),
        ("llm_max_concurrency", 0),
        ("embedding_batch_size", 0),
        ("embedding_max_concurrency", 0),
        ("recall_vector_top_k", 0),
        ("recall_lexical_top_k", 0),
        ("recall_graph_seed_top_k", 0),
        ("recall_graph_depth", 0),
        ("recall_graph_max_nodes", 0),
        ("recall_default_top_k", 0),
        ("recall_max_top_k", 0),
        ("recall_rrf_k", 0),
        ("worker_poll_interval_ms", 0),
        ("worker_stale_after_seconds", 0),
        ("worker_max_concurrent_datasets", 0),
        ("worker_max_concurrent_reads", 0),
    ],
)
def test_negative_or_zero_values_are_rejected(field_name: str, value: int) -> None:
    assert_invalid(**{field_name: value})


def test_default_top_k_greater_than_max_top_k_is_rejected() -> None:
    assert_invalid(recall_default_top_k=101, recall_max_top_k=100)


def test_cors_empty_string_disables_cors() -> None:
    settings = make_settings(cors_allowed_origins="")

    assert settings.cors_allowed_origins == ()


def test_cors_comma_separated_values_are_normalized() -> None:
    settings = make_settings(cors_allowed_origins="https://a.example, https://b.example")

    assert settings.cors_allowed_origins == ("https://a.example", "https://b.example")


def test_secrets_do_not_appear_in_repr() -> None:
    settings = make_settings()
    rendered = repr(settings)

    assert VALID_API_KEY not in rendered
    assert VALID_LLM_API_KEY not in rendered
    assert VALID_NEO4J_PASSWORD not in rendered
    assert VALID_DATABASE_URL not in rendered
    assert "**********" in rendered


def test_env_file_can_be_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"API_KEY={VALID_API_KEY}",
                f"DATABASE_URL={VALID_DATABASE_URL}",
                f"NEO4J_PASSWORD={VALID_NEO4J_PASSWORD}",
                f"LLM_API_KEY={VALID_LLM_API_KEY}",
                "APP_NAME=Loaded From Env",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.app_name == "Loaded From Env"


def test_env_file_can_disable_cors_with_empty_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"API_KEY={VALID_API_KEY}",
                f"DATABASE_URL={VALID_DATABASE_URL}",
                f"NEO4J_PASSWORD={VALID_NEO4J_PASSWORD}",
                f"LLM_API_KEY={VALID_LLM_API_KEY}",
                "CORS_ALLOWED_ORIGINS=",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.cors_allowed_origins == ()


def test_env_file_with_utf8_bom_can_be_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"API_KEY={VALID_API_KEY}",
                f"DATABASE_URL={VALID_DATABASE_URL}",
                f"NEO4J_PASSWORD={VALID_NEO4J_PASSWORD}",
                f"LLM_API_KEY={VALID_LLM_API_KEY}",
            ]
        ),
        encoding="utf-8-sig",
    )

    settings = load_settings(env_file)

    assert settings.api_key.get_secret_value() == VALID_API_KEY


def test_environment_values_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", VALID_API_KEY)
    monkeypatch.setenv("DATABASE_URL", VALID_DATABASE_URL)
    monkeypatch.setenv("NEO4J_PASSWORD", VALID_NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_API_KEY", VALID_LLM_API_KEY)
    monkeypatch.setenv("HTTP_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.http_port == 9000


def test_settings_does_not_alter_global_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOFIAS_MEMORY_TEST_MARKER", "unchanged")
    before = dict(os.environ)

    make_settings()

    assert os.environ["SOFIAS_MEMORY_TEST_MARKER"] == "unchanged"
    assert dict(os.environ) == before


def test_two_independent_instances_can_be_built() -> None:
    first = make_settings(app_name="First")
    second = make_settings(app_name="Second")

    assert first.app_name == "First"
    assert second.app_name == "Second"


def test_unknown_os_environment_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_SYSTEM_VARIABLE", "ignored")

    settings = make_settings()

    assert settings.app_name == "Sofias Memory"


def test_env_file_unknown_setting_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"API_KEY={VALID_API_KEY}",
                f"DATABASE_URL={VALID_DATABASE_URL}",
                f"NEO4J_PASSWORD={VALID_NEO4J_PASSWORD}",
                f"LLM_API_KEY={VALID_LLM_API_KEY}",
                "SOFIAS_MEMORY_UNKNOWN_SETTING=value",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_settings(env_file)


def test_unknown_env_file_variable_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"API_KEY={VALID_API_KEY}",
                f"DATABASE_URL={VALID_DATABASE_URL}",
                f"NEO4J_PASSWORD={VALID_NEO4J_PASSWORD}",
                f"LLM_API_KEY={VALID_LLM_API_KEY}",
                "APP_NMAE=typo",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_settings(env_file)


def test_invalid_urls_are_rejected() -> None:
    assert_invalid(database_url="not-a-postgres-url")
    assert_invalid(neo4j_uri="http://neo4j:7687")
    assert_invalid(llm_base_url="not-a-url")
    assert_invalid(embedding_base_url="ftp://example.test")


def test_settings_are_immutable() -> None:
    settings = make_settings()

    with pytest.raises(ValidationError):
        settings.app_name = "Changed"


def test_same_functional_configuration_has_same_fingerprint() -> None:
    assert make_settings().config_fingerprint() == make_settings().config_fingerprint()


def test_independent_instances_have_same_fingerprint() -> None:
    first = make_settings()
    second = make_settings()

    assert first is not second
    assert first.config_fingerprint() == second.config_fingerprint()


def test_app_version_change_does_not_change_fingerprint() -> None:
    baseline = make_settings(app_version="0.1.0")
    changed = make_settings(app_version="0.1.1")

    assert changed.config_fingerprint() == baseline.config_fingerprint()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("llm_model", "gpt-5"),
        ("embedding_model", "text-embedding-3-small"),
        ("embedding_dimensions", 1536),
        ("chunk_max_tokens", 901),
        ("chunk_overlap_tokens", 121),
        ("recall_vector_top_k", 51),
        ("recall_lexical_top_k", 51),
        ("recall_graph_seed_top_k", 11),
        ("recall_graph_depth", 3),
        ("recall_graph_max_nodes", 101),
        ("recall_default_top_k", 13),
        ("recall_max_top_k", 101),
        ("recall_rrf_k", 61),
    ],
)
def test_functional_configuration_changes_fingerprint(field_name: str, value: object) -> None:
    baseline = make_settings().config_fingerprint()
    changed = make_settings(**{field_name: value}).config_fingerprint()

    assert changed != baseline


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("api_key", f"{API_KEY_PREFIX}{'b' * 32}"),
        ("llm_api_key", "sk-different-fake-llm-key"),
        ("embedding_api_key", "sk-different-fake-embedding-key"),
        (
            "database_url",
            "postgresql+asyncpg://sofias_memory:different@postgres:5432/sofias_memory",
        ),
        ("neo4j_password", "different-fake-neo4j-password"),
        ("database_pool_size", 20),
        ("database_max_overflow", 20),
        ("worker_max_concurrent_datasets", 2),
        ("worker_max_concurrent_reads", 16),
        ("http_port", 9000),
        ("cors_allowed_origins", "https://app.example"),
        ("data_directory", Path("/different/sources")),
        ("temp_directory", Path("/different/tmp")),
        ("store_query_content", False),
        ("log_document_content", True),
        ("log_llm_payloads", True),
    ],
)
def test_non_functional_configuration_does_not_change_fingerprint(
    field_name: str, value: object
) -> None:
    baseline = make_settings().config_fingerprint()
    changed = make_settings(**{field_name: value}).config_fingerprint()

    assert changed == baseline


def test_fingerprint_is_sha256_hex() -> None:
    fingerprint = make_settings().config_fingerprint()

    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


def test_build_config_fingerprint_matches_settings_method() -> None:
    settings = make_settings()

    assert build_config_fingerprint(settings) == settings.config_fingerprint()


def test_fingerprint_payload_has_schema_version_and_empty_prompt_versions() -> None:
    payload = build_config_fingerprint_payload(make_settings())

    assert payload["schema_version"] == FINGERPRINT_SCHEMA_VERSION
    assert "application" not in payload
    assert payload["prompt_versions"] == {}


def test_prompt_versions_are_deterministic_when_provided() -> None:
    settings = make_settings()

    first = settings.config_fingerprint(prompt_versions={"b": "2", "a": "1"})
    second = settings.config_fingerprint(prompt_versions={"a": "1", "b": "2"})

    assert first == second


def test_changing_prompt_versions_changes_fingerprint() -> None:
    settings = make_settings()

    assert settings.config_fingerprint() != settings.config_fingerprint(
        prompt_versions={"future_prompt": "v1"}
    )


def test_canonical_payload_is_deterministic() -> None:
    first = build_config_fingerprint_payload(make_settings())
    second = build_config_fingerprint_payload(make_settings())

    assert canonical_config_fingerprint_payload(first) == canonical_config_fingerprint_payload(
        second
    )


def test_secrets_do_not_appear_in_fingerprint_payload_or_canonical_payload() -> None:
    settings = make_settings(
        api_key=f"{API_KEY_PREFIX}{'b' * 32}",
        database_url="postgresql+asyncpg://sofias_memory:known-db-secret@postgres:5432/db",
        neo4j_password="known-neo4j-secret",
        llm_api_key="known-llm-secret",
        embedding_api_key="known-embedding-secret",
    )
    payload = build_config_fingerprint_payload(settings)
    canonical_payload = canonical_config_fingerprint_payload(payload)

    for secret in [
        f"{API_KEY_PREFIX}{'b' * 32}",
        "known-db-secret",
        "known-neo4j-secret",
        "known-llm-secret",
        "known-embedding-secret",
    ]:
        assert secret not in str(payload)
        assert secret not in canonical_payload


def test_urls_with_credentials_are_sanitized_in_fingerprint_payload() -> None:
    settings = make_settings(
        llm_base_url="https://llm-user:llm-password@api.openai.com/v1",
        embedding_base_url="https://embedding-user:embedding-password@embeddings.example/v1",
    )
    payload = build_config_fingerprint_payload(settings)
    canonical_payload = canonical_config_fingerprint_payload(payload)

    assert payload["llm"] == {
        "base_url": "https://api.openai.com/v1",
        "model": settings.llm_model,
    }
    assert payload["embeddings"] == {
        "base_url": "https://embeddings.example/v1",
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }
    assert "llm-user" not in canonical_payload
    assert "llm-password" not in canonical_payload
    assert "embedding-user" not in canonical_payload
    assert "embedding-password" not in canonical_payload
