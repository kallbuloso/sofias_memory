"""QA-01: migrations/env.py must not reconfigure host logging when Alembic is
run programmatically with ``config.attributes["configure_logger"] = False``,
while the default (Alembic CLI) path keeps calling ``logging.config.fileConfig``.

Runs Alembic in offline (``--sql``) mode up to the first revision only, so no
database connection is opened; all configuration values are synthetic.
"""

from __future__ import annotations

import json
import logging
import logging.config
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from sofias_memory.observability.logging import clear_log_context, configure_logging, get_logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
PROBE_LOGGER_NAME = "sofias_memory.qa01_offline_env_probe"


@pytest.fixture()
def offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Synthetic settings only; chdir so load_settings() never reads a local .env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", f"sf-{'A' * 32}")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://synthetic:synthetic@localhost:5432/synthetic"
    )
    monkeypatch.setenv("NEO4J_PASSWORD", "synthetic-neo4j-password")
    monkeypatch.setenv("LLM_API_KEY", "sk-synthetic")
    monkeypatch.setenv(
        "COGNITIVE_IDEMPOTENCY_HMAC_KEY", "synthetic-cognitive-idempotency-hmac-key-0123456789"
    )


def run_offline_upgrade_to_first_revision(config: Config) -> None:
    base_revision = ScriptDirectory.from_config(config).get_base()
    assert base_revision is not None
    command.upgrade(config, base_revision, sql=True)


def spy_on_file_config(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def recording_file_config(fname: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append(str(fname))

    # env.py binds "from logging.config import fileConfig" each time Alembic
    # loads it, so patching the module attribute is observed by env.py.
    monkeypatch.setattr(logging.config, "fileConfig", recording_file_config)
    return calls


@pytest.mark.usefixtures("offline_env")
def test_default_configuration_still_invokes_file_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = spy_on_file_config(monkeypatch)

    run_offline_upgrade_to_first_revision(Config(str(ALEMBIC_INI)))

    capsys.readouterr()
    assert calls == [str(ALEMBIC_INI)]


@pytest.mark.usefixtures("offline_env")
def test_configure_logger_false_skips_file_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = spy_on_file_config(monkeypatch)
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False

    run_offline_upgrade_to_first_revision(config)

    capsys.readouterr()
    assert calls == []


@pytest.mark.usefixtures("offline_env")
def test_programmatic_run_preserves_loggers_handlers_and_structured_events(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    probe_logger = logging.getLogger(PROBE_LOGGER_NAME)
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    level_before = root_logger.level
    assert any(isinstance(handler, type(caplog.handler)) for handler in handlers_before)

    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False
    run_offline_upgrade_to_first_revision(config)
    capsys.readouterr()

    assert probe_logger.disabled is False
    assert root_logger.handlers == handlers_before
    assert root_logger.level == level_before

    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    get_logger(PROBE_LOGGER_NAME).info("qa01_probe_event", step="probe")
    clear_log_context()

    records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    assert [record["event"] for record in records] == ["qa01_probe_event"]
    assert records[0]["step"] == "probe"
    assert records[0]["level"] == "info"
