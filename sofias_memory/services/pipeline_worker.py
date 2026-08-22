"""Internal worker coordinator (ADR-0009 SS T/U/G/H, SM-505).

Connects the PostgreSQL queue (SM-503's :class:`PipelineRunClaimer`) to the
resumable pipeline engine (SM-504's :class:`PipelineEngine`) inside the same
FastAPI process. A poll loop claims eligible ``PipelineRun`` rows up to a
configured capacity, dispatches each to a tracked execution task that runs
the engine to its own conclusion while a sibling heartbeat task keeps
PostgreSQL's ownership lease fresh, and a cooperative shutdown sequence stops
new claims, lets in-flight steps reach their next safe checkpoint within a
bounded grace period, and never leaves an ``asyncio.Task`` unawaited.

PostgreSQL remains the queue, the lifecycle authority, and the durability
boundary throughout -- ``asyncio`` here is only a local execution mechanism
(ADR-0009 "Decision"). This module does not implement stale recovery
(SM-507) or autonomous ``graph_outbox`` processing (SM-506); it only knows
how to run claimed work to completion or to a safe pause point, on a
schedule PostgreSQL owns.
"""

from __future__ import annotations

import asyncio
import contextlib

from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.pipelines.engine import PipelineEngine
from sofias_memory.pipelines.registry import PipelineRegistry
from sofias_memory.services.pipeline_queue_claimer import (
    ClaimedRun,
    PipelineRunClaimer,
    new_worker_id,
)

logger = get_logger(__name__)

DEFAULT_WORKER_SHUTDOWN_GRACE_SECONDS = 30.0
"""ADR-0009 SS T requires a bounded shutdown grace period but freezes no
exact value. Not a ``Settings`` field (backlog SS 23: no new configuration
without a concrete need) -- a documented, test-overridable code constant."""

HEARTBEAT_INTERVAL_FRACTION = 1.0 / 3.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.25
MAX_HEARTBEAT_INTERVAL_SECONDS = 30.0


def heartbeat_interval_seconds(stale_after_seconds: int) -> float:
    """ADR-0009 SS H: a fixed fraction of ``WORKER_STALE_AFTER_SECONDS``,
    bounded to a sane floor/ceiling, so heartbeat cadence stays materially
    shorter than the stale threshold for every valid ``Settings`` value
    (``WORKER_STALE_AFTER_SECONDS`` is a positive integer, minimum ``1``)."""

    return min(
        MAX_HEARTBEAT_INTERVAL_SECONDS,
        max(
            MIN_HEARTBEAT_INTERVAL_SECONDS,
            stale_after_seconds * HEARTBEAT_INTERVAL_FRACTION,
        ),
    )


class PipelineWorkerCoordinator:
    """Poll, claim, dispatch, heartbeat, and cooperative-shutdown orchestration.

    One instance owns exactly one ``worker_id`` (ADR-0009 SS G), generated
    once at construction time -- the identity of one process-boot's worker,
    reused for every run it claims during its lifetime. A new instance (a
    real restart, or a fresh instance in a test) always gets a new
    ``worker_id``.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: PipelineRegistry,
        *,
        enabled: bool,
        poll_interval_ms: int,
        stale_after_seconds: int,
        max_concurrent_datasets: int,
        claimer: PipelineRunClaimer | None = None,
        engine: PipelineEngine | None = None,
        shutdown_grace_seconds: float = DEFAULT_WORKER_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._enabled = enabled
        self._poll_interval_seconds = poll_interval_ms / 1000.0
        self._heartbeat_interval_seconds = heartbeat_interval_seconds(stale_after_seconds)
        self._max_concurrent_datasets = max_concurrent_datasets
        self._claimer = claimer or PipelineRunClaimer(session_factory)
        self._engine = engine or PipelineEngine(session_factory, registry)
        self._shutdown_grace_seconds = shutdown_grace_seconds

        self.worker_id = new_worker_id()

        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_claims: dict[asyncio.Task[None], ClaimedRun] = {}
        self._started = False
        self._stopped = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        """Minimal worker readiness signal (ADR-0009 SS U, SM-505 SS 37):
        ``True`` only once :meth:`start` has completed and :meth:`stop` has
        not (yet) been called. Never inspects run/task internals -- that is
        SM-516 observability, not this story's readiness contract."""

        return self._started and not self._stopped

    async def start(self) -> None:
        """No-op when disabled (ADR-0009 SS U) or already started -- calling
        ``start()`` twice never creates a second poll task."""

        if not self._enabled or self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="pipeline-worker-poll")

    async def stop(self) -> None:
        """No-op when disabled or never started. Otherwise: stop issuing new
        claims, let in-flight execution tasks reach a safe checkpoint within
        the bounded grace period, forcibly cancel and await whatever remains,
        and attempt one final best-effort heartbeat for still-owned runs
        before returning (ADR-0009 SS T, SM-505 SS 22-25)."""

        if not self._enabled or not self._started or self._stopped:
            return
        self._stopped = True
        self._stop_event.set()

        poll_task = self._poll_task
        if poll_task is not None:
            await poll_task
            self._poll_task = None

        claims_snapshot = list(self._active_claims.values())
        if self._active_tasks:
            _done, pending = await asyncio.wait(
                self._active_tasks, timeout=self._shutdown_grace_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._active_tasks.clear()
            self._active_claims.clear()

        await self._final_heartbeat_sweep(claims_snapshot)

    # -- poll loop ------------------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._fill_capacity()
            except Exception as exc:  # noqa: BLE001 - SM-505 SS 29 infra-failure containment
                logger.warning(
                    "pipeline_worker_claim_scan_failed",
                    worker_id=self.worker_id,
                    exception_type=type(exc).__name__,
                )
            await self._wait_for_next_poll_or_stop()

    async def _wait_for_next_poll_or_stop(self) -> None:
        """Interruptible poll wait (SM-505 SS 11): wakes immediately on
        shutdown instead of always sleeping the full poll interval."""

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval_seconds)

    async def _fill_capacity(self) -> None:
        """Claim repeatedly until capacity is full or no candidate remains
        (SM-505 SS 12/13). An empty registry means no pipeline is executable
        at all -- the worker stays idle and never calls the claimer for one
        (SM-505 SS 30/31), rather than claiming a run it cannot execute."""

        if len(self._registry) == 0:
            return
        while len(self._active_tasks) < self._max_concurrent_datasets:
            if self._stop_event.is_set():
                # SM-505 SS 11/26: stop issuing *new* claim attempts once the
                # shutdown signal is observed. A claim already awaited below
                # this check when the signal arrived is allowed to finish and
                # be dispatched -- see the claim-in-progress race note below.
                return
            claimed = await self._claimer.try_claim_one(worker_id=self.worker_id)
            if claimed is None:
                return
            # SM-505 SS 26: even if the stop signal arrived while the claim
            # above was in flight, a successful claim is never discarded --
            # it is always dispatched as a tracked execution task, which will
            # itself observe the shutdown signal at its own next safe
            # checkpoint.
            self._dispatch(claimed)

    def _dispatch(self, claimed: ClaimedRun) -> None:
        task = asyncio.create_task(
            self._run_claimed(claimed), name=f"pipeline-worker-run-{claimed.run_id}"
        )
        self._active_tasks.add(task)
        self._active_claims[task] = claimed
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._active_tasks.discard(task)
        self._active_claims.pop(task, None)

    # -- per-run execution wrapper ---------------------------------------

    async def _run_claimed(self, claimed: ClaimedRun) -> None:
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claimed), name=f"pipeline-worker-heartbeat-{claimed.run_id}"
        )
        try:
            await self._engine.execute(claimed, stop_requested=self._stop_event.is_set)
        except Exception as exc:  # noqa: BLE001 - SM-505 SS 28 unexpected-failure containment
            # Deliberately not `asyncio.CancelledError` (it is BaseException,
            # not Exception, so it always propagates -- SM-505 SS 24). An
            # engine/step failure this broad has already exhausted every
            # typed classification the engine itself understands; it is
            # consumed here so one problematic run can never take down the
            # poll loop or any other run.
            logger.warning(
                "pipeline_worker_run_failed_unexpectedly",
                run_id=str(claimed.run_id),
                worker_id=claimed.worker_id,
                exception_type=type(exc).__name__,
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    # -- heartbeat --------------------------------------------------------

    async def _heartbeat_loop(self, claimed: ClaimedRun) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            owned = await self._heartbeat_once(claimed)
            if not owned:
                # ADR-0009 SS H/SM-505 SS 9: a guarded update returning False
                # is ordinary ownership/status loss (terminal, requeued, or
                # superseded) -- stop heartbeating this run, not an error.
                return

    async def _heartbeat_once(self, claimed: ClaimedRun) -> bool:
        try:
            async with PostgresUnitOfWork(self._session_factory) as uow:
                owned = await uow.pipeline_runs.heartbeat_if_owned(
                    claimed.run_id,
                    worker_id=claimed.worker_id,
                    attempt=claimed.attempt,
                )
                await uow.commit()
                return owned
        except Exception as exc:  # noqa: BLE001 - SM-505 SS 27 transient-heartbeat containment
            logger.warning(
                "pipeline_worker_heartbeat_failed",
                run_id=str(claimed.run_id),
                worker_id=claimed.worker_id,
                exception_type=type(exc).__name__,
            )
            # A transient heartbeat failure is not proof of ownership loss --
            # keep trying at the next cadence while the run is still tracked.
            return True

    async def _final_heartbeat_sweep(self, claims: list[ClaimedRun]) -> None:
        """SM-505 SS 25: one best-effort final heartbeat per still-tracked
        claim before DB resources close, so ``heartbeat_at`` reflects
        approximately when this process actually stopped being able to
        continue the work. Never marks success/failure; a guarded update
        that no longer matches (terminal, superseded) is silently a no-op."""

        for claimed in claims:
            await self._heartbeat_once(claimed)


__all__ = [
    "DEFAULT_WORKER_SHUTDOWN_GRACE_SECONDS",
    "HEARTBEAT_INTERVAL_FRACTION",
    "MAX_HEARTBEAT_INTERVAL_SECONDS",
    "MIN_HEARTBEAT_INTERVAL_SECONDS",
    "PipelineWorkerCoordinator",
    "heartbeat_interval_seconds",
]
