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
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.pipelines.engine import PipelineEngine
from sofias_memory.pipelines.registry import PipelineRegistry
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
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

DEFAULT_GRAPH_OUTBOX_BATCH_SIZE = 50
"""Bounded per-poll-tick claim-and-process burst for the autonomous graph
outbox consumer (ADR-0009 SS V, backlog SS 12). Each iteration claims,
applies, and finalizes exactly one row (short lease, no batch of rows left
PROCESSING at once) -- not a single query claiming many rows up front."""


@dataclass(frozen=True, slots=True)
class WorkerHealthSnapshot:
    """Internal-only runtime health signal (SM-516 SS 7). Not exposed on any
    public schema -- readiness collapses this into a plain ``ready`` boolean
    with a safe generic detail (SS 37)."""

    enabled: bool
    started: bool
    stopped: bool
    poll_task_alive: bool
    outbox_task_expected: bool
    outbox_task_alive: bool
    active_run_count: int
    operational: bool


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
        graph_outbox_processor: GraphOutboxProcessor | None = None,
        resources: Mapping[str, Any] | None = None,
    ) -> None:
        """``resources`` is the explicitly-populated
        :attr:`PipelineContext.resources` map handed to the engine this
        coordinator builds (LLM/embedding/summary clients, SM-510). Ignored
        when an ``engine`` is injected -- that engine already carries its own
        resources. Graph projection is not among them: it converges through
        ``graph_outbox_processor``'s own autonomous loop, never from inside a
        pipeline step."""

        self._session_factory = session_factory
        self._registry = registry
        self._enabled = enabled
        self._poll_interval_seconds = poll_interval_ms / 1000.0
        self._heartbeat_interval_seconds = heartbeat_interval_seconds(stale_after_seconds)
        self._max_concurrent_datasets = max_concurrent_datasets
        self._claimer = claimer or PipelineRunClaimer(session_factory)
        self._engine = engine or PipelineEngine(session_factory, registry, resources=resources)
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._graph_outbox_processor = graph_outbox_processor

        self.worker_id = new_worker_id()

        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_claims: dict[asyncio.Task[None], ClaimedRun] = {}
        self._started = False
        self._stopped = False

        self._outbox_task: asyncio.Task[None] | None = None
        self._poll_task_failed = False
        self._outbox_task_failed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        """Minimal started/not-stopped lifecycle signal (ADR-0009 SS U,
        SM-505 SS 37). No longer the "is the worker available for new
        work" contract as of the SM-516 staging fix below -- that is
        :attr:`is_operational` now, the single signal both readiness and
        new-work gating share. ``is_running`` stays as a plain lifecycle
        flag for internal coordinator tests/introspection only."""

        return self._started and not self._stopped

    @property
    def is_operational(self) -> bool:
        """SM-516 staging fix (worker-availability split-brain): the single
        "worker available for NEW work" signal, backed by the exact same
        predicate readiness uses (:meth:`health_snapshot`.``operational``).
        A dead poll/outbox task now correctly blocks new-run creation, not
        just readiness -- ``is_running`` alone could stay ``True`` after an
        unexpected background-task death, silently accepting writes a dead
        worker could never claim."""

        return self.health_snapshot().operational

    def health_snapshot(self) -> WorkerHealthSnapshot:
        """SM-516 SS 6-7: a worker is not operational merely because it was
        started -- it must also still have a live poll loop, and a live
        outbox loop when one is expected. A single ``PipelineRun``'s
        engine-level failure never flips this (SS 6): only the coordinator's
        own background tasks dying unexpectedly does."""

        poll_alive = self._started and not self._stopped and not self._poll_task_failed
        outbox_expected = self._graph_outbox_processor is not None
        outbox_alive = not outbox_expected or (
            self._started and not self._stopped and not self._outbox_task_failed
        )
        return WorkerHealthSnapshot(
            enabled=self._enabled,
            started=self._started,
            stopped=self._stopped,
            poll_task_alive=poll_alive,
            outbox_task_expected=outbox_expected,
            outbox_task_alive=outbox_alive,
            active_run_count=len(self._active_tasks),
            operational=self._enabled and poll_alive and outbox_alive,
        )

    async def start(self) -> None:
        """No-op when disabled (ADR-0009 SS U) or already started -- calling
        ``start()`` twice never creates a second poll task. Task creation
        failures are logged and re-raised (SM-516 SS 9) rather than left as a
        silently half-started coordinator -- including a *partial* failure,
        where the poll task was already created successfully and only the
        graph outbox task's own creation raises: that already-live poll task
        must never be left running as an orphan (SM-516 staging fix,
        Scenario B)."""

        if not self._enabled or self._started:
            return
        logger.info("worker_starting", worker_id=self.worker_id)
        try:
            self._started = True
            self._stop_event.clear()
            self._poll_task_failed = False
            self._outbox_task_failed = False
            self._poll_task = asyncio.create_task(self._poll_loop(), name="pipeline-worker-poll")
            self._poll_task.add_done_callback(self._on_poll_task_done)
            if self._graph_outbox_processor is not None:
                self._outbox_task = asyncio.create_task(
                    self._outbox_loop(), name="pipeline-worker-graph-outbox"
                )
                self._outbox_task.add_done_callback(self._on_outbox_task_done)
        except Exception as exc:
            await self._cleanup_partial_start()
            logger.error(
                "worker_start_failed",
                worker_id=self.worker_id,
                exception_type=type(exc).__name__,
            )
            raise
        logger.info("worker_started", worker_id=self.worker_id)

    async def _cleanup_partial_start(self) -> None:
        """Cancel and await whatever task(s) a failed :meth:`start` already
        created before the failure, then reset to a fully-stopped state.
        Never leaves an earlier successfully-created task running just
        because a later one failed."""

        self._started = False
        self._stop_event.set()
        partial_tasks = [task for task in (self._poll_task, self._outbox_task) if task is not None]
        for task in partial_tasks:
            task.cancel()
        if partial_tasks:
            await asyncio.gather(*partial_tasks, return_exceptions=True)
        self._poll_task = None
        self._outbox_task = None

    async def stop(self) -> None:
        """No-op when disabled or never started. Otherwise: stop issuing new
        claims, let in-flight execution tasks reach a safe checkpoint within
        the bounded grace period, forcibly cancel and await whatever remains,
        and attempt one final best-effort heartbeat for still-owned runs
        before returning (ADR-0009 SS T, SM-505 SS 22-25)."""

        if not self._enabled or not self._started or self._stopped:
            return
        shutdown_start = time.monotonic()
        active_runs_at_shutdown = len(self._active_tasks)
        logger.info(
            "worker_stopping",
            worker_id=self.worker_id,
            active_runs=active_runs_at_shutdown,
        )
        self._stopped = True
        self._stop_event.set()

        poll_task = self._poll_task
        if poll_task is not None:
            # SM-516 SS 8: the poll task may already have died unexpectedly
            # (``_on_poll_task_done`` already observed/logged that). Awaiting
            # a task that completed with an exception re-raises it -- shut
            # down must stay deterministic even for an already-dead task.
            await asyncio.gather(poll_task, return_exceptions=True)
            self._poll_task = None

        outbox_task = self._outbox_task
        if outbox_task is not None:
            _done, pending = await asyncio.wait({outbox_task}, timeout=self._shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._outbox_task = None

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
        duration_ms = (time.monotonic() - shutdown_start) * 1000
        logger.info(
            "worker_stopped",
            worker_id=self.worker_id,
            active_runs=active_runs_at_shutdown,
            duration_ms=round(duration_ms, 2),
        )

    # -- background task death observability ------------------------------

    def _on_poll_task_done(self, task: asyncio.Task[None]) -> None:
        """SM-516 SS 8: the poll loop is designed to run until
        ``_stop_event`` is set, swallowing per-tick failures internally
        (``_poll_loop``). If it ends any other way -- an exception escaped
        its own structure, or it returned before shutdown was requested --
        that is the coordinator's own failure, not a business run failure."""

        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._poll_task_failed = True
            logger.error(
                "worker_background_task_failed",
                worker_id=self.worker_id,
                task_name="pipeline-worker-poll",
                exception_type=type(exc).__name__,
            )
        elif not self._stop_event.is_set():
            self._poll_task_failed = True
            logger.error(
                "worker_background_task_failed",
                worker_id=self.worker_id,
                task_name="pipeline-worker-poll",
                exception_type="UnexpectedExit",
            )

    def _on_outbox_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._outbox_task_failed = True
            logger.error(
                "worker_background_task_failed",
                worker_id=self.worker_id,
                task_name="pipeline-worker-graph-outbox",
                exception_type=type(exc).__name__,
            )
        elif not self._stop_event.is_set():
            self._outbox_task_failed = True
            logger.error(
                "worker_background_task_failed",
                worker_id=self.worker_id,
                task_name="pipeline-worker-graph-outbox",
                exception_type="UnexpectedExit",
            )

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

    # -- autonomous graph outbox consumer (SM-506) ------------------------

    async def _outbox_loop(self) -> None:
        """Independent of the pipeline claim loop and of registry contents
        (backlog SS 30): even with an empty ``PipelineRegistry``, a pending
        ``graph_outbox`` row must still converge autonomously."""

        while not self._stop_event.is_set():
            try:
                await self._drain_outbox_burst()
            except Exception as exc:  # noqa: BLE001 - SS 37 infra-failure containment
                # A projection failure already marked its own row FAILED
                # (GraphOutboxProcessor); this only stops the current burst
                # early so a persistently unavailable Neo4j cannot busy-spin
                # the remaining attempt budget within one tick (backlog SS 13).
                logger.warning(
                    "graph_outbox_autonomous_burst_failed",
                    worker_id=self.worker_id,
                    exception_type=type(exc).__name__,
                )
            await self._wait_for_next_poll_or_stop()

    async def _drain_outbox_burst(self) -> None:
        assert self._graph_outbox_processor is not None  # noqa: S101 - only called when set
        for _ in range(DEFAULT_GRAPH_OUTBOX_BATCH_SIZE):
            if self._stop_event.is_set():
                return
            result = await self._graph_outbox_processor.claim_and_process_one()
            if result is None:
                return

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
    "DEFAULT_GRAPH_OUTBOX_BATCH_SIZE",
    "DEFAULT_WORKER_SHUTDOWN_GRACE_SECONDS",
    "HEARTBEAT_INTERVAL_FRACTION",
    "MAX_HEARTBEAT_INTERVAL_SECONDS",
    "MIN_HEARTBEAT_INTERVAL_SECONDS",
    "PipelineWorkerCoordinator",
    "WorkerHealthSnapshot",
    "heartbeat_interval_seconds",
]
