"""Unit tests for operational metrics aggregation and the periodic reporter
(SM-516 SS 18-21, 53). Real-Postgres query correctness is covered by
``tests/integration/test_operational_metrics_postgres_integration.py``; here
we test the pure aggregation helpers and the reporter's lifecycle/failure
handling with a fake metrics service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from io import StringIO

import pytest

from sofias_memory.domain.enums import GraphOutboxStatus, PipelineRunStatus
from sofias_memory.observability.logging import clear_log_context, configure_logging
from sofias_memory.services.operational_metrics import (
    GraphOutboxCounts,
    OperationalMetricsReporter,
    OperationalMetricsSnapshot,
    RunCountsByStatus,
    _graph_outbox_counts_from_rows,
    _run_counts_from_rows,
)

TEST_TIMEOUT = 5.0


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()
    httpx_logger.setLevel(previous_httpx_level)


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


# --- pure aggregation helpers -------------------------------------------------


def test_run_counts_from_rows_defaults_missing_statuses_to_zero() -> None:
    counts = _run_counts_from_rows([(PipelineRunStatus.QUEUED, 3)])
    assert counts == RunCountsByStatus(queued=3)


def test_run_counts_from_rows_all_statuses() -> None:
    rows = [
        (PipelineRunStatus.QUEUED, 1),
        (PipelineRunStatus.RUNNING, 2),
        (PipelineRunStatus.SUCCEEDED, 3),
        (PipelineRunStatus.FAILED, 4),
        (PipelineRunStatus.CANCELLING, 5),
        (PipelineRunStatus.CANCELLED, 6),
    ]
    counts = _run_counts_from_rows(rows)
    assert counts == RunCountsByStatus(
        queued=1, running=2, succeeded=3, failed=4, cancelling=5, cancelled=6
    )


def test_graph_outbox_counts_distinguishes_retryable_from_ceiling() -> None:
    rows = [
        (GraphOutboxStatus.PENDING, 0, 2),
        (GraphOutboxStatus.PROCESSING, 1, 1),
        (GraphOutboxStatus.DONE, 1, 10),
        (GraphOutboxStatus.FAILED, 2, 3),  # below ceiling -- retryable
        (GraphOutboxStatus.FAILED, 5, 1),  # at ceiling
        (GraphOutboxStatus.FAILED, 7, 1),  # past ceiling -- still "at ceiling"
    ]
    counts = _graph_outbox_counts_from_rows(rows, max_attempts=5)
    assert counts == GraphOutboxCounts(
        pending=2, processing=1, done=10, failed_retryable=3, failed_at_ceiling=2
    )


def test_graph_outbox_counts_empty_rows_all_zero() -> None:
    assert _graph_outbox_counts_from_rows([], max_attempts=5) == GraphOutboxCounts()


# --- reporter lifecycle/failure handling -------------------------------------


@dataclass
class FakeMetricsService:
    snapshots: list[OperationalMetricsSnapshot | Exception] = field(default_factory=list)
    calls: int = 0

    async def collect(self) -> OperationalMetricsSnapshot:
        self.calls += 1
        outcome = self.snapshots.pop(0) if self.snapshots else _empty_snapshot()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _empty_snapshot() -> OperationalMetricsSnapshot:
    return OperationalMetricsSnapshot(
        run_counts=RunCountsByStatus(),
        runs_queued_total=0,
        runs_queued_eligible=0,
        heartbeat_stale_count=0,
        operational_missing_heartbeat=0,
        graph_outbox=GraphOutboxCounts(),
    )


@pytest.mark.asyncio
async def test_reporter_emits_snapshot_event_on_start(log_stream: StringIO) -> None:
    service = FakeMetricsService()
    reporter = OperationalMetricsReporter(service, interval_seconds=100.0)  # type: ignore[arg-type]
    await asyncio.wait_for(reporter.start(), timeout=TEST_TIMEOUT)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(reporter.stop(), timeout=TEST_TIMEOUT)

    events = [r["event"] for r in read_log_records(log_stream)]
    assert "operational_metrics_snapshot" in events
    assert service.calls == 1


@pytest.mark.asyncio
async def test_reporter_collection_failure_logs_safely_and_keeps_looping(
    log_stream: StringIO,
) -> None:
    service = FakeMetricsService(snapshots=[RuntimeError("simulated transient DB failure")])
    reporter = OperationalMetricsReporter(service, interval_seconds=0.01)  # type: ignore[arg-type]
    await asyncio.wait_for(reporter.start(), timeout=TEST_TIMEOUT)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if service.calls >= 2:
            break
    await asyncio.wait_for(reporter.stop(), timeout=TEST_TIMEOUT)

    records = read_log_records(log_stream)
    events = [r["event"] for r in records]
    assert "operational_metrics_snapshot_failed" in events
    failed_record = next(r for r in records if r["event"] == "operational_metrics_snapshot_failed")
    assert failed_record["exception_type"] == "RuntimeError"
    assert "simulated transient DB failure" not in json.dumps(failed_record)
    # The loop kept going after the failure (service.calls advanced past 1).
    assert service.calls >= 2


@pytest.mark.asyncio
async def test_reporter_double_start_is_a_no_op() -> None:
    service = FakeMetricsService()
    reporter = OperationalMetricsReporter(service, interval_seconds=100.0)  # type: ignore[arg-type]
    await asyncio.wait_for(reporter.start(), timeout=TEST_TIMEOUT)
    first_task = reporter._task  # noqa: SLF001 - white-box lifecycle assertion
    await reporter.start()
    assert reporter._task is first_task  # noqa: SLF001
    await asyncio.wait_for(reporter.stop(), timeout=TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_reporter_stop_before_start_is_a_no_op() -> None:
    service = FakeMetricsService()
    reporter = OperationalMetricsReporter(service)  # type: ignore[arg-type]
    await asyncio.wait_for(reporter.stop(), timeout=TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_reporter_stop_leaves_no_tracked_task(log_stream: StringIO) -> None:
    del log_stream
    service = FakeMetricsService()
    reporter = OperationalMetricsReporter(service, interval_seconds=100.0)  # type: ignore[arg-type]
    await asyncio.wait_for(reporter.start(), timeout=TEST_TIMEOUT)
    await asyncio.wait_for(reporter.stop(), timeout=TEST_TIMEOUT)
    assert reporter._task is None  # noqa: SLF001
