from __future__ import annotations

import json
from io import StringIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.lifespan import app_settings
from sofias_memory.observability.logging import clear_log_context, configure_logging

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_name": "Sofias Memory Test",
        "app_version": "9.8.7",
        "app_env": "test",
        "log_level": "INFO",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture()
def log_stream(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    stream = StringIO()

    def configure_logging_for_test(log_level: str | int) -> None:
        configure_logging(log_level, stream=stream)

    clear_log_context()
    monkeypatch.setattr("sofias_memory.lifespan.configure_logging", configure_logging_for_test)
    yield stream
    clear_log_context()


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_lifespan_executes_startup(log_stream: StringIO) -> None:
    with TestClient(create_app(make_settings())):
        records = read_log_records(log_stream)

    assert records[0]["event"] == "application_starting"


def test_lifespan_executes_shutdown(log_stream: StringIO) -> None:
    with TestClient(create_app(make_settings())):
        pass

    records = read_log_records(log_stream)
    assert records[-1]["event"] == "application_shutdown"


def test_lifespan_configures_logging_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | int] = []

    def configure_logging_spy(log_level: str | int) -> None:
        calls.append(log_level)
        configure_logging(log_level, stream=StringIO())

    monkeypatch.setattr("sofias_memory.lifespan.configure_logging", configure_logging_spy)

    with TestClient(create_app(make_settings(log_level="WARNING"))):
        pass

    assert calls == ["WARNING"]


def test_startup_log_contains_safe_metadata(log_stream: StringIO) -> None:
    settings = make_settings()

    with TestClient(create_app(settings)):
        records = read_log_records(log_stream)

    startup = records[0]
    assert startup["app_name"] == settings.app_name
    assert startup["app_version"] == settings.app_version
    assert startup["app_env"] == settings.app_env
    assert startup["config_fingerprint"] == settings.config_fingerprint()


def test_startup_and_shutdown_logs_do_not_contain_secrets(log_stream: StringIO) -> None:
    settings = make_settings(
        api_key="sf-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        database_url=(
            f"postgresql+asyncpg://sofias_memory:{KNOWN_SECRET}@postgres:5432/sofias_memory"
        ),
        neo4j_password=KNOWN_SECRET,
        llm_api_key=KNOWN_SECRET,
    )

    with TestClient(create_app(settings)):
        pass

    output = log_stream.getvalue()
    assert KNOWN_SECRET not in output
    assert settings.api_key.get_secret_value() not in output


def test_shutdown_log_is_emitted(log_stream: StringIO) -> None:
    with TestClient(create_app(make_settings())):
        pass

    assert any(record["event"] == "application_shutdown" for record in read_log_records(log_stream))


def test_app_settings_returns_configured_settings_instance() -> None:
    settings = make_settings()
    app = create_app(settings)

    assert app_settings(app) is settings


def test_app_settings_fails_when_settings_are_missing() -> None:
    app = FastAPI()

    with pytest.raises(RuntimeError, match="application settings"):
        app_settings(app)


def test_create_app_does_not_configure_logging_before_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(log_level: str | int) -> None:
        raise AssertionError(f"unexpected logging configuration: {log_level}")

    monkeypatch.setattr("sofias_memory.lifespan.configure_logging", fail_if_called)

    create_app(make_settings())


def test_create_app_uses_injected_settings_without_loading_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called() -> Settings:
        raise AssertionError("load_settings should not be called")

    monkeypatch.setattr("sofias_memory.app.load_settings", fail_if_called)

    app = create_app(make_settings())

    assert isinstance(app, FastAPI)
