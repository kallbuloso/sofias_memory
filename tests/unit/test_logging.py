from __future__ import annotations

import asyncio
import json
from io import StringIO

import pytest
from pydantic import SecretStr

from sofias_memory.observability.logging import (
    REDACTED,
    bind_log_context,
    bound_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
    redact_sensitive_data,
    unbind_log_context,
)

KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_configure_logging_outputs_json_event(log_stream: StringIO) -> None:
    get_logger("sofias_memory.tests").info("logging_configured")

    [record] = read_log_records(log_stream)
    assert record["event"] == "logging_configured"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_log_level_is_respected() -> None:
    stream = StringIO()
    configure_logging("WARNING", stream=stream)

    get_logger("sofias_memory.tests").info("hidden_event")
    get_logger("sofias_memory.tests").warning("visible_event")

    [record] = read_log_records(stream)
    assert record["event"] == "visible_event"
    assert record["level"] == "warning"


def test_bind_request_id(log_stream: StringIO) -> None:
    bind_log_context(request_id="request-1")

    get_logger("sofias_memory.tests").info("request_bound")

    [record] = read_log_records(log_stream)
    assert record["request_id"] == "request-1"


def test_bind_run_id(log_stream: StringIO) -> None:
    bind_log_context(run_id="run-1")

    get_logger("sofias_memory.tests").info("run_bound")

    [record] = read_log_records(log_stream)
    assert record["run_id"] == "run-1"


def test_bind_dataset_id(log_stream: StringIO) -> None:
    bind_log_context(dataset_id="dataset-1")

    get_logger("sofias_memory.tests").info("dataset_bound")

    [record] = read_log_records(log_stream)
    assert record["dataset_id"] == "dataset-1"


def test_bind_source_document_and_step(log_stream: StringIO) -> None:
    bind_log_context(source_id="source-1", document_id="document-1", step="chunk")

    get_logger("sofias_memory.tests").info("pipeline_step")

    [record] = read_log_records(log_stream)
    assert record["source_id"] == "source-1"
    assert record["document_id"] == "document-1"
    assert record["step"] == "chunk"


def test_bind_worker_pipeline_type_and_attempt(log_stream: StringIO) -> None:
    bind_log_context(worker_id="wk-1", pipeline_type="cognify", attempt=2)

    get_logger("sofias_memory.tests").info("worker_run_bound")

    [record] = read_log_records(log_stream)
    assert record["worker_id"] == "wk-1"
    assert record["pipeline_type"] == "cognify"
    assert record["attempt"] == 2


def test_unbind_log_context_removes_selected_field(log_stream: StringIO) -> None:
    bind_log_context(request_id="request-1", run_id="run-1")
    unbind_log_context("run_id")

    get_logger("sofias_memory.tests").info("request_only")

    [record] = read_log_records(log_stream)
    assert record["request_id"] == "request-1"
    assert "run_id" not in record


def test_clear_log_context_removes_previous_fields(log_stream: StringIO) -> None:
    bind_log_context(request_id="request-1")
    clear_log_context()

    get_logger("sofias_memory.tests").info("context_cleared")

    [record] = read_log_records(log_stream)
    assert "request_id" not in record


@pytest.mark.asyncio
async def test_contextvars_do_not_leak_between_tasks(log_stream: StringIO) -> None:
    async def log_with_request_id(request_id: str) -> None:
        bind_log_context(request_id=request_id)
        await asyncio.sleep(0)
        get_logger("sofias_memory.tests").info("task_event")
        clear_log_context()

    await asyncio.gather(log_with_request_id("request-a"), log_with_request_id("request-b"))

    records = read_log_records(log_stream)
    assert {record["request_id"] for record in records} == {"request-a", "request-b"}


@pytest.mark.asyncio
async def test_worker_run_context_does_not_leak_between_concurrent_runs(
    log_stream: StringIO,
) -> None:
    """SM-516 SS 14/52: two concurrently executing worker runs (each binding
    its own run/step-scoped context, mirroring ``PipelineEngine.execute``'s
    ``bound_log_context`` usage) must never see each other's fields, and a
    run's context must never survive into an unrelated later worker log."""

    async def run_with_context(run_id: str, step: str) -> None:
        with bound_log_context(run_id=run_id, worker_id="wk-shared", attempt=1):
            await asyncio.sleep(0)
            with bound_log_context(step=step):
                await asyncio.sleep(0)
                get_logger("sofias_memory.tests").info("pipeline_step_event")

    await asyncio.gather(
        run_with_context("run-a", "extract"),
        run_with_context("run-b", "summarize"),
    )

    records = {record["run_id"]: record for record in read_log_records(log_stream)}
    assert records["run-a"]["step"] == "extract"
    assert records["run-b"]["step"] == "summarize"
    assert records["run-a"]["worker_id"] == "wk-shared"
    assert records["run-b"]["worker_id"] == "wk-shared"

    get_logger("sofias_memory.tests").info("worker_idle_after_runs")
    idle_record = read_log_records(log_stream)[-1]
    assert "run_id" not in idle_record
    assert "step" not in idle_record


@pytest.mark.parametrize(
    "field_name",
    [
        "API_KEY",
        "api_key",
        "X-API-Key",
        "x-api-key",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "DATABASE_URL",
        "NEO4J_PASSWORD",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    ],
)
def test_sensitive_field_names_are_redacted(field_name: str) -> None:
    redacted = redact_sensitive_data({field_name: KNOWN_SECRET})

    assert redacted == {field_name: REDACTED}
    assert KNOWN_SECRET not in str(redacted)


def test_nested_password_is_redacted() -> None:
    redacted = redact_sensitive_data(
        {
            "headers": {"X-API-Key": KNOWN_SECRET},
            "database": {"password": KNOWN_SECRET},
        }
    )

    assert redacted == {
        "headers": {"X-API-Key": REDACTED},
        "database": {"password": REDACTED},
    }
    assert KNOWN_SECRET not in str(redacted)


def test_secrets_inside_nested_dict_are_redacted() -> None:
    redacted = redact_sensitive_data({"outer": {"inner": {"llm_api_key": KNOWN_SECRET}}})

    assert redacted == {"outer": {"inner": {"llm_api_key": REDACTED}}}


def test_secrets_inside_list_and_tuple_are_redacted() -> None:
    redacted = redact_sensitive_data(
        {
            "items": [
                {"api_key": KNOWN_SECRET},
                ({"refresh_token": KNOWN_SECRET}, {"safe": "visible"}),
            ]
        }
    )

    assert redacted == {
        "items": [
            {"api_key": REDACTED},
            ({"refresh_token": REDACTED}, {"safe": "visible"}),
        ]
    }
    assert KNOWN_SECRET not in str(redacted)


def test_secret_str_is_never_revealed() -> None:
    redacted = redact_sensitive_data({"safe_name": SecretStr(KNOWN_SECRET)})

    assert redacted == {"safe_name": REDACTED}
    assert KNOWN_SECRET not in str(redacted)


def test_url_credentials_are_redacted_even_for_non_secret_field() -> None:
    redacted = redact_sensitive_data(
        {"connection": f"postgresql://sofias:{KNOWN_SECRET}@postgres/sofias_memory"}
    )

    assert redacted == {"connection": "postgresql://sofias:***@postgres/sofias_memory"}
    assert KNOWN_SECRET not in str(redacted)


def test_url_query_secret_is_redacted_even_for_non_secret_field() -> None:
    redacted = redact_sensitive_data(
        {"callback": f"https://example.test/path?token={KNOWN_SECRET}"}
    )

    assert redacted == {"callback": f"https://example.test/path?token={REDACTED}"}
    assert KNOWN_SECRET not in str(redacted)


def test_non_sensitive_structure_remains_intact() -> None:
    value = {
        "dataset_id": "dataset-1",
        "duration_ms": 12.5,
        "metadata": {"name": "visible", "top_k": 10},
        "tags": ["a", "b"],
    }

    assert redact_sensitive_data(value) == value


def test_document_and_llm_content_fields_are_redacted() -> None:
    redacted = redact_sensitive_data(
        {
            "document_content": KNOWN_SECRET,
            "embedding": [0.1, 0.2],
            "llm_request_payload": {"messages": [KNOWN_SECRET]},
        }
    )

    assert redacted == {
        "document_content": REDACTED,
        "embedding": REDACTED,
        "llm_request_payload": REDACTED,
    }
    assert KNOWN_SECRET not in str(redacted)


def test_repeated_configure_logging_does_not_duplicate_output() -> None:
    stream = StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("INFO", stream=stream)

    get_logger("sofias_memory.tests").info("single_output")

    records = read_log_records(stream)
    assert len(records) == 1
    assert records[0]["event"] == "single_output"


def test_logger_works_after_repeated_configuration() -> None:
    stream = StringIO()
    configure_logging("ERROR", stream=stream)
    configure_logging("INFO", stream=stream)

    get_logger("sofias_memory.tests").info("still_working")

    [record] = read_log_records(stream)
    assert record["event"] == "still_working"


def test_known_secret_does_not_appear_in_captured_log_output(log_stream: StringIO) -> None:
    get_logger("sofias_memory.tests").info(
        "secret_event",
        API_KEY=KNOWN_SECRET,
        nested={"password": KNOWN_SECRET},
        callback=f"https://example.test/path?access_token={KNOWN_SECRET}",
    )

    output = log_stream.getvalue()
    [record] = read_log_records(log_stream)
    assert KNOWN_SECRET not in output
    assert record["API_KEY"] == REDACTED
    assert record["nested"] == {"password": REDACTED}
    assert record["callback"] == f"https://example.test/path?access_token={REDACTED}"
