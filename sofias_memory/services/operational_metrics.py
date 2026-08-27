"""Narrow, PostgreSQL-derived operational metrics (SM-516 SS 18-19, 26-28).

Observability only: this module never mutates ``pipeline_runs`` or
``graph_outbox`` state, and a failure collecting a snapshot never changes
business state or readiness (SS 29, 53) -- that stays the exclusive job of
the real dependency-health checkers (``PostgresReadinessChecker``,
``Neo4jReadinessChecker``) and the worker's own health snapshot.

Every predicate here reuses the exact same status/time semantics the
authoritative code already uses elsewhere (SS 26): the stale-heartbeat
predicate matches ``PipelineRunRepository.list_stale_candidate_ids`` (ADR-
0009 SS H/SS I), the claim-eligible predicate matches the queue claimer, and
the graph outbox ceiling matches ``GraphOutboxProcessor``'s own
``max_attempts`` check. Nothing here invents a second "stale status" or a
new policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select

from sofias_memory.domain.enums import GraphOutboxStatus, PipelineRunStatus
from sofias_memory.infrastructure.postgres.models.graph_outbox import GraphOutbox
from sofias_memory.infrastructure.postgres.models.pipeline_run import PipelineRun
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.observability.logging import get_logger
from sofias_memory.services.graph_outbox_processor import DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS

logger = get_logger(__name__)

STALE_RUN_STATUSES = (PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)
"""ADR-0009 SS H: only RUNNING/CANCELLING runs can ever be heartbeat-stale."""

DEFAULT_METRICS_REPORT_INTERVAL_SECONDS = 60.0
"""SS 19: "a fixed conservative interval as a code constant is preferable to
adding another Settings field" -- not configurable without a concrete need."""


@dataclass(frozen=True, slots=True)
class RunCountsByStatus:
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelling: int = 0
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class GraphOutboxCounts:
    pending: int = 0
    processing: int = 0
    done: int = 0
    failed_retryable: int = 0
    failed_at_ceiling: int = 0


@dataclass(frozen=True, slots=True)
class OperationalMetricsSnapshot:
    run_counts: RunCountsByStatus
    runs_queued_total: int
    """SS 28: every ``QUEUED`` run, including one still awaiting a future
    scheduled retry (``next_attempt_at`` in the future)."""

    runs_queued_eligible: int
    """SS 28: ``QUEUED`` runs the claimer could pick up right now --
    ``next_attempt_at`` is ``NULL`` or already due. A future-scheduled
    automatic retry is never counted as claimable (SS 28)."""

    heartbeat_stale_count: int
    """RUNNING/CANCELLING runs whose ``heartbeat_at`` already satisfies the
    same predicate startup recovery uses (SS 26). Excludes legacy
    NULL-heartbeat rows (SS 26/ADR-0009 SS I) -- those are startup-recovery
    only, tracked separately below."""

    operational_missing_heartbeat: int
    """RUNNING/CANCELLING rows with a NULL ``heartbeat_at`` -- counted
    separately from ordinary staleness (SS 18), never folded into
    ``heartbeat_stale_count``."""

    graph_outbox: GraphOutboxCounts


class OperationalMetricsService:
    """Collects one :class:`OperationalMetricsSnapshot` per call. Stateless
    and read-only -- every query is a plain ``SELECT``, no row locking, no
    write, safe to call concurrently with the worker and with itself."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        stale_after_seconds: float,
        graph_outbox_max_attempts: int = DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self._stale_after_seconds = stale_after_seconds
        self._graph_outbox_max_attempts = graph_outbox_max_attempts

    async def collect(self) -> OperationalMetricsSnapshot:
        async with self._session_factory() as session:
            run_status_rows = await session.execute(
                select(PipelineRun.status, func.count()).group_by(PipelineRun.status)
            )
            run_counts = _run_counts_from_rows(run_status_rows.all())

            eligible_predicate = or_(
                PipelineRun.next_attempt_at.is_(None),
                PipelineRun.next_attempt_at <= func.now(),
            )
            runs_queued_eligible = await session.scalar(
                select(func.count()).where(
                    PipelineRun.status == PipelineRunStatus.QUEUED,
                    eligible_predicate,
                )
            )

            stale_cutoff = func.now() - func.make_interval(
                0, 0, 0, 0, 0, 0, self._stale_after_seconds
            )
            heartbeat_stale_count = await session.scalar(
                select(func.count()).where(
                    PipelineRun.status.in_(STALE_RUN_STATUSES),
                    PipelineRun.heartbeat_at.is_not(None),
                    PipelineRun.heartbeat_at < stale_cutoff,
                )
            )
            operational_missing_heartbeat = await session.scalar(
                select(func.count()).where(
                    PipelineRun.status.in_(STALE_RUN_STATUSES),
                    PipelineRun.heartbeat_at.is_(None),
                )
            )

            outbox_status_rows = await session.execute(
                select(GraphOutbox.status, GraphOutbox.attempt, func.count()).group_by(
                    GraphOutbox.status, GraphOutbox.attempt
                )
            )
            graph_outbox = _graph_outbox_counts_from_rows(
                outbox_status_rows.all(), max_attempts=self._graph_outbox_max_attempts
            )

        return OperationalMetricsSnapshot(
            run_counts=run_counts,
            runs_queued_total=run_counts.queued,
            runs_queued_eligible=int(runs_queued_eligible or 0),
            heartbeat_stale_count=int(heartbeat_stale_count or 0),
            operational_missing_heartbeat=int(operational_missing_heartbeat or 0),
            graph_outbox=graph_outbox,
        )


def _run_counts_from_rows(rows: Sequence[Any]) -> RunCountsByStatus:
    counts = {status: 0 for status in PipelineRunStatus}
    for status, count in rows:
        counts[status] = int(count)
    return RunCountsByStatus(
        queued=counts[PipelineRunStatus.QUEUED],
        running=counts[PipelineRunStatus.RUNNING],
        succeeded=counts[PipelineRunStatus.SUCCEEDED],
        failed=counts[PipelineRunStatus.FAILED],
        cancelling=counts[PipelineRunStatus.CANCELLING],
        cancelled=counts[PipelineRunStatus.CANCELLED],
    )


def _graph_outbox_counts_from_rows(rows: Sequence[Any], *, max_attempts: int) -> GraphOutboxCounts:
    pending = processing = done = failed_retryable = failed_at_ceiling = 0
    for status, attempt, count in rows:
        count = int(count)
        if status == GraphOutboxStatus.PENDING:
            pending += count
        elif status == GraphOutboxStatus.PROCESSING:
            processing += count
        elif status == GraphOutboxStatus.DONE:
            done += count
        elif status == GraphOutboxStatus.FAILED:
            if attempt >= max_attempts:
                failed_at_ceiling += count
            else:
                failed_retryable += count
    return GraphOutboxCounts(
        pending=pending,
        processing=processing,
        done=done,
        failed_retryable=failed_retryable,
        failed_at_ceiling=failed_at_ceiling,
    )


class OperationalMetricsReporter:
    """SS 19-21, 29, 53: a small in-process reporter that periodically logs
    one ``operational_metrics_snapshot`` event. Purely observational -- never
    the authority for anything, never touched by readiness, and a collection
    failure only logs a safe warning and waits for the next interval (no
    busy-spin, no readiness impact, no business-state change). Its lifecycle
    is tracked/awaited exactly like the worker's own background tasks, but
    it is never coupled to worker enablement/claiming (SS 20) -- local
    operational visibility should stay available even in a disabled/
    read-only degraded deployment."""

    def __init__(
        self,
        metrics_service: OperationalMetricsService,
        *,
        interval_seconds: float = DEFAULT_METRICS_REPORT_INTERVAL_SECONDS,
    ) -> None:
        self._metrics_service = metrics_service
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="operational-metrics-reporter")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._report_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)

    async def _report_once(self) -> None:
        try:
            snapshot = await self._metrics_service.collect()
        except Exception as exc:  # noqa: BLE001 - SS 53 observability-only containment
            logger.warning(
                "operational_metrics_snapshot_failed",
                exception_type=type(exc).__name__,
            )
            return

        logger.info(
            "operational_metrics_snapshot",
            runs_queued=snapshot.run_counts.queued,
            runs_running=snapshot.run_counts.running,
            runs_succeeded=snapshot.run_counts.succeeded,
            runs_failed=snapshot.run_counts.failed,
            runs_cancelling=snapshot.run_counts.cancelling,
            runs_cancelled=snapshot.run_counts.cancelled,
            runs_queued_total=snapshot.runs_queued_total,
            runs_queued_eligible=snapshot.runs_queued_eligible,
            heartbeat_stale_count=snapshot.heartbeat_stale_count,
            operational_missing_heartbeat=snapshot.operational_missing_heartbeat,
            graph_outbox_pending=snapshot.graph_outbox.pending,
            graph_outbox_processing=snapshot.graph_outbox.processing,
            graph_outbox_done=snapshot.graph_outbox.done,
            graph_outbox_failed_retryable=snapshot.graph_outbox.failed_retryable,
            graph_outbox_failed_at_ceiling=snapshot.graph_outbox.failed_at_ceiling,
        )


__all__ = [
    "DEFAULT_METRICS_REPORT_INTERVAL_SECONDS",
    "GraphOutboxCounts",
    "OperationalMetricsReporter",
    "OperationalMetricsService",
    "OperationalMetricsSnapshot",
    "RunCountsByStatus",
    "STALE_RUN_STATUSES",
]
