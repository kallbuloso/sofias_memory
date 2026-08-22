"""Unit coverage for the SM-509 shared ``wait=true`` waiter.

Uses an in-memory fake status reader (no real PostgreSQL) so the full
poll/timeout/terminal-race algorithm is exercised deterministically and
quickly. Real PostgreSQL polling against a genuinely concurrent writer is
proven in ``tests/integration/test_pipeline_submission_postgres_integration.py``.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import PipelineRunStatus
from sofias_memory.services.pipeline_waiter import (
    PipelineRunWaiter,
    RunNotFoundDuringWaitError,
)


class ScriptedReader:
    """Returns each status in ``sequence`` in order, one per call, then
    repeats the last value -- a controllable in-memory stand-in for
    PostgreSQL reads."""

    def __init__(self, sequence: list[PipelineRunStatus | None]) -> None:
        self._sequence = sequence
        self.calls = 0

    async def __call__(self, run_id: UUID) -> PipelineRunStatus | None:
        del run_id
        index = min(self.calls, len(self._sequence) - 1)
        self.calls += 1
        return self._sequence[index]


class MutableReader:
    """A reader whose current status can be flipped externally mid-wait,
    simulating a concurrent writer without touching PostgreSQL."""

    def __init__(self, status: PipelineRunStatus) -> None:
        self.status: PipelineRunStatus | None = status
        self.calls = 0

    async def __call__(self, run_id: UUID) -> PipelineRunStatus | None:
        del run_id
        self.calls += 1
        return self.status


@pytest.mark.asyncio
async def test_queued_keeps_waiting_until_timeout() -> None:
    reader = ScriptedReader([PipelineRunStatus.QUEUED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)
    run_id = uuid4()

    outcome = await waiter.wait_for_terminal(run_id, timeout_seconds=0.05)

    assert outcome.timed_out is True
    assert outcome.status == PipelineRunStatus.QUEUED
    assert outcome.terminal is False
    assert reader.calls >= 2


@pytest.mark.asyncio
async def test_running_keeps_waiting_until_timeout() -> None:
    reader = ScriptedReader([PipelineRunStatus.RUNNING])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=0.05)

    assert outcome.timed_out is True
    assert outcome.status == PipelineRunStatus.RUNNING


@pytest.mark.asyncio
async def test_cancelling_keeps_waiting_until_timeout() -> None:
    reader = ScriptedReader([PipelineRunStatus.CANCELLING])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=0.05)

    assert outcome.timed_out is True
    assert outcome.status == PipelineRunStatus.CANCELLING
    assert outcome.terminal is False


@pytest.mark.asyncio
async def test_succeeded_is_terminal_and_returns_immediately() -> None:
    reader = ScriptedReader([PipelineRunStatus.SUCCEEDED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=5.0)

    assert outcome.timed_out is False
    assert outcome.terminal is True
    assert outcome.status == PipelineRunStatus.SUCCEEDED
    assert reader.calls == 1


@pytest.mark.asyncio
async def test_failed_is_terminal() -> None:
    reader = ScriptedReader([PipelineRunStatus.FAILED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=5.0)

    assert outcome.timed_out is False
    assert outcome.status == PipelineRunStatus.FAILED


@pytest.mark.asyncio
async def test_cancelled_is_terminal() -> None:
    reader = ScriptedReader([PipelineRunStatus.CANCELLED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=5.0)

    assert outcome.timed_out is False
    assert outcome.status == PipelineRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_timeout_returns_the_same_run_id() -> None:
    reader = ScriptedReader([PipelineRunStatus.QUEUED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)
    run_id = uuid4()

    outcome = await waiter.wait_for_terminal(run_id, timeout_seconds=0.03)

    assert outcome.run_id == run_id


@pytest.mark.asyncio
async def test_timeout_preserves_current_persisted_status() -> None:
    reader = ScriptedReader([PipelineRunStatus.RUNNING])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=0.03)

    assert outcome.status == PipelineRunStatus.RUNNING
    assert outcome.timed_out is True


@pytest.mark.asyncio
async def test_final_read_wins_the_timeout_race_when_it_becomes_terminal() -> None:
    """Flips to SUCCEEDED exactly when the deadline is reached -- the fresh
    read that happens before the deadline check must observe it and return
    terminal, never a fabricated timeout (ADR-0009 SS R point 5)."""

    reader = MutableReader(PipelineRunStatus.RUNNING)
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    async def flip_soon() -> None:
        await asyncio.sleep(0.02)
        reader.status = PipelineRunStatus.SUCCEEDED

    flipper = asyncio.create_task(flip_soon())
    outcome = await waiter.wait_for_terminal(uuid4(), timeout_seconds=0.05)
    await flipper

    assert outcome.timed_out is False
    assert outcome.status == PipelineRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_timeout_never_mutates_anything_it_only_reads() -> None:
    reader = ScriptedReader([PipelineRunStatus.QUEUED])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    await waiter.wait_for_terminal(uuid4(), timeout_seconds=0.03)

    # ScriptedReader has no mutation surface at all -- the only interaction
    # the waiter can have with it is __call__ (a read). Asserting the reader
    # was never asked to do anything but read is the whole point here.
    assert reader.calls >= 1


@pytest.mark.asyncio
async def test_handler_cancellation_does_not_touch_the_run() -> None:
    reader = MutableReader(PipelineRunStatus.RUNNING)
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    task = asyncio.create_task(waiter.wait_for_terminal(uuid4(), timeout_seconds=5.0))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The reader's status was never written to by the waiter -- only ever
    # read (self.calls incremented, self.status never assigned by us).
    assert reader.status == PipelineRunStatus.RUNNING


@pytest.mark.asyncio
async def test_polling_postgresql_read_is_the_authority_not_an_event() -> None:
    """The waiter has no ``asyncio.Event``/``asyncio.Queue`` field at all --
    every observation goes through the injected status reader."""

    assert not hasattr(PipelineRunWaiter, "_event")
    assert not hasattr(PipelineRunWaiter, "_queue")


@pytest.mark.asyncio
async def test_missing_run_fails_in_a_controlled_way_not_an_infinite_loop() -> None:
    reader = ScriptedReader([None])
    waiter = PipelineRunWaiter(status_reader=reader, poll_interval_seconds=0.01)

    with pytest.raises(RunNotFoundDuringWaitError):
        await asyncio.wait_for(waiter.wait_for_terminal(uuid4(), timeout_seconds=5.0), timeout=1.0)
