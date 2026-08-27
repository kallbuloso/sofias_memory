"""Real-PostgreSQL tests for the operational metrics snapshot (SM-516 SS 18,
58-59). Read-only (no row locking, no mutation), so -- like the readiness
checkers -- this runs directly against the configured dev database, no
dedicated discardable database required.
"""

from __future__ import annotations

import os

import pytest

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.engine import create_async_engine_from_settings
from sofias_memory.services.operational_metrics import OperationalMetricsService

OPERATIONAL_METRICS_ENV = "SOFIAS_MEMORY_RUN_OPERATIONAL_METRICS_POSTGRES_TESTS"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_operational_metrics_snapshot_against_real_database() -> None:
    if os.environ.get(OPERATIONAL_METRICS_ENV) != "1":
        pytest.skip(f"set {OPERATIONAL_METRICS_ENV}=1 to run operational metrics tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        session_factory = create_session_factory(engine)
        service = OperationalMetricsService(
            session_factory,
            stale_after_seconds=float(settings.worker_stale_after_seconds),
        )

        snapshot = await service.collect()
    finally:
        await dispose_async_engine(engine)

    counts = snapshot.run_counts
    for value in (
        counts.queued,
        counts.running,
        counts.succeeded,
        counts.failed,
        counts.cancelling,
        counts.cancelled,
        snapshot.runs_queued_total,
        snapshot.runs_queued_eligible,
        snapshot.heartbeat_stale_count,
        snapshot.operational_missing_heartbeat,
        snapshot.graph_outbox.pending,
        snapshot.graph_outbox.processing,
        snapshot.graph_outbox.done,
        snapshot.graph_outbox.failed_retryable,
        snapshot.graph_outbox.failed_at_ceiling,
    ):
        assert value >= 0

    # SS 28: eligible QUEUED runs are always a subset of every QUEUED run.
    assert snapshot.runs_queued_eligible <= snapshot.runs_queued_total
    assert snapshot.runs_queued_total == counts.queued
