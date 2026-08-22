"""Shared ``wait=true`` waiter for B5 write endpoints (ADR-0009 SS R, SM-509).

PostgreSQL is the sole authority for whether a run has reached a terminal
state (SS R point 2): this waiter polls the persisted ``PipelineRun`` row on
a short interval and never depends on an in-memory ``asyncio.Event``/
``asyncio.Queue``/process-local signal as its *only* source of completion.
Each poll is a short, independent read -- never a transaction held open
across ``asyncio.sleep`` -- so a concurrent writer (the worker claiming or
finishing the run) is always free to update the row while this waiter
sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sofias_memory.domain import TERMINAL_RUN_STATUSES, PipelineRunStatus
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork

StatusReader = Callable[[UUID], Coroutine[None, None, PipelineRunStatus | None]]
"""``(run_id) -> current status, or None if the run does not exist``. Each
call must be one short, independent PostgreSQL read (SM-509 Part N) -- never
a transaction held across the waiter's own ``asyncio.sleep``. Injectable so
unit tests can exercise the full poll/timeout/race algorithm against an
in-memory fake, while production uses a real, short-lived
``PostgresUnitOfWork`` read per call (the same fake-vs-real split as
``PipelineSubmissionService``, SM-509)."""

DEFAULT_POLL_INTERVAL_SECONDS = 0.05
"""Short by design (SM-509 Part P): a constant, not a new environment
setting -- ``Settings.request_wait_timeout_seconds`` is the only wait-related
setting this story introduces/reuses."""


class RunNotFoundDuringWaitError(Exception):
    """The run being waited on no longer exists in PostgreSQL.

    A controlled failure (SM-509 Part M item 45), never an infinite poll
    loop: this can only happen if the caller passes a ``run_id`` that was
    never durably committed, since a real submission always commits its
    ``PipelineRun`` row before any waiter could ever be started on it.
    """

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"PipelineRun {run_id} does not exist.")


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """What :meth:`PipelineRunWaiter.wait_for_terminal` observed. Never
    mutates the run: a timeout outcome carries whatever status was most
    recently read, nothing more."""

    run_id: UUID
    status: PipelineRunStatus
    timed_out: bool

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


class PipelineRunWaiter:
    """Polls PostgreSQL for a run to reach a terminal state, bounded by a
    caller-supplied timeout (``Settings.request_wait_timeout_seconds``)."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        status_reader: StatusReader | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if session_factory is None and status_reader is None:
            raise ValueError("session_factory or status_reader is required")
        self._read_status = status_reader or _postgres_status_reader(
            cast(AsyncSessionFactory, session_factory)
        )
        self._poll_interval_seconds = poll_interval_seconds

    async def wait_for_terminal(self, run_id: UUID, *, timeout_seconds: float) -> WaitOutcome:
        """Poll until ``run_id`` reaches a terminal state or ``timeout_seconds``
        elapses (measured on the monotonic event-loop clock, never
        ``datetime.now()``).

        If the handler running this coroutine is cancelled (client
        disconnect), ``asyncio.CancelledError`` propagates normally -- the
        run itself is never touched here regardless of how this coroutine
        exits (ADR-0009 SS R point 6, SM-509 Part Q): every read in this loop
        is a plain ``SELECT``, never a mutation.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        while True:
            status = await self._read_status(run_id)
            if status is None:
                raise RunNotFoundDuringWaitError(run_id)
            if status in TERMINAL_RUN_STATUSES:
                return WaitOutcome(run_id=run_id, status=status, timed_out=False)

            remaining = deadline - loop.time()
            if remaining <= 0:
                # A fresh read just happened above and was still
                # non-terminal, so the timeout genuinely wins this
                # iteration; if the row flips terminal after this instant,
                # the *next* call to this method (not this one) would see
                # it -- there is no more time budget left to poll again here.
                return WaitOutcome(run_id=run_id, status=status, timed_out=True)

            await asyncio.sleep(min(self._poll_interval_seconds, remaining))


def _postgres_status_reader(session_factory: AsyncSessionFactory) -> StatusReader:
    async def read_status(run_id: UUID) -> PipelineRunStatus | None:
        """One short, independent read -- no ``FOR UPDATE``, no transaction
        held across the caller's sleep (SM-509 Part N)."""

        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            return run.status if run is not None else None

    return read_status
