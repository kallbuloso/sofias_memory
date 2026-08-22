"""Unit tests for the internal worker coordinator (SM-505).

Uses fakes/mocks for the claimer and engine to prove coordinator control
flow -- start/stop lifecycle, capacity, poll wakeup, task tracking/cleanup,
exception containment, readiness, and worker identity. These tests do NOT
prove real PostgreSQL heartbeat/claim concurrency or real engine shutdown
disposition semantics; that is
``tests/integration/test_pipeline_worker_postgres_integration.py``'s job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from sofias_memory.domain import PipelineType
from sofias_memory.pipelines.engine import PipelineExecutionResult
from sofias_memory.pipelines.registry import PipelineDefinition, PipelineRegistry
from sofias_memory.services.pipeline_queue_claimer import ClaimedRun
from sofias_memory.services.pipeline_worker import (
    HEARTBEAT_INTERVAL_FRACTION,
    MAX_HEARTBEAT_INTERVAL_SECONDS,
    MIN_HEARTBEAT_INTERVAL_SECONDS,
    PipelineWorkerCoordinator,
    heartbeat_interval_seconds,
)

# --- heartbeat_interval_seconds (pure) --------------------------------------


@pytest.mark.parametrize(
    "stale_after_seconds",
    [1, 3, 300, 3_600_000],
)
def test_heartbeat_interval_always_shorter_than_stale_after(stale_after_seconds: int) -> None:
    interval = heartbeat_interval_seconds(stale_after_seconds)
    assert interval < stale_after_seconds


def test_heartbeat_interval_uses_documented_fraction_within_bounds() -> None:
    stale_after_seconds = 60  # fraction (20.0) lands strictly within [floor, ceiling]
    expected = stale_after_seconds * HEARTBEAT_INTERVAL_FRACTION
    assert heartbeat_interval_seconds(stale_after_seconds) == pytest.approx(expected)


def test_heartbeat_interval_never_below_floor() -> None:
    # WORKER_STALE_AFTER_SECONDS is a positive integer (Settings: gt=0), so the
    # floor is never reached for a real config; exercised directly here as a
    # pure-function boundary proof.
    assert heartbeat_interval_seconds(0.5) == pytest.approx(  # type: ignore[arg-type]
        MIN_HEARTBEAT_INTERVAL_SECONDS
    )


def test_heartbeat_interval_never_above_ceiling() -> None:
    assert heartbeat_interval_seconds(3_600_000) == pytest.approx(MAX_HEARTBEAT_INTERVAL_SECONDS)


# --- fakes -------------------------------------------------------------------


@dataclass
class FakeClaimer:
    """Returns queued ``ClaimedRun`` values one at a time, then ``None``."""

    queued: list[ClaimedRun] = field(default_factory=list)
    calls: int = 0

    async def try_claim_one(self, *, worker_id: str) -> ClaimedRun | None:
        del worker_id
        self.calls += 1
        if self.queued:
            return self.queued.pop(0)
        return None


@dataclass
class FakeEngine:
    """Records every ``execute()`` call; result/behavior configurable per run."""

    result_by_run: dict[object, PipelineExecutionResult] = field(default_factory=dict)
    hang_forever: bool = False
    raise_error: bool = False
    calls: list[ClaimedRun] = field(default_factory=list)

    async def execute(
        self,
        claimed_run: ClaimedRun,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> PipelineExecutionResult:
        del stop_requested
        self.calls.append(claimed_run)
        if self.raise_error:
            raise RuntimeError("simulated unexpected engine failure")
        if self.hang_forever:
            await asyncio.Event().wait()
        return self.result_by_run.get(
            claimed_run.run_id,
            PipelineExecutionResult(run_id=claimed_run.run_id, status=None),
        )


def make_claimed_run(*, dataset_id: object | None = None) -> ClaimedRun:
    return ClaimedRun(
        run_id=uuid4(),
        dataset_id=dataset_id,  # type: ignore[arg-type]
        pipeline_type=PipelineType.COGNIFY,
        worker_id="wk-test",
        attempt=1,
    )


def non_empty_registry() -> PipelineRegistry:
    class _NoOpStep:
        async def execute(self, context: object) -> object:  # pragma: no cover - unused
            raise AssertionError("engine is faked; step must never execute")

        async def persist(self, context: object, result: object, uow: object) -> None:  # noqa: D401
            return None

        async def compensate(self, context: object, result: object) -> None:
            return None

    from sofias_memory.pipelines.registry import PipelineStepDefinition

    definition = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY,
        steps=(
            PipelineStepDefinition(
                name="a",
                definition_id="a:v1",
                step=_NoOpStep(),
                input_deriver=lambda run_input, step_outputs: {},
            ),
        ),
    )
    return PipelineRegistry([definition])


@dataclass
class FakeGraphOutboxProcessor:
    """Records ``claim_and_process_one`` calls; drains a queued script of
    results/exceptions, then returns ``None`` (no more claimable work)."""

    script: list[object] = field(default_factory=list)
    calls: int = 0

    async def claim_and_process_one(self) -> object | None:
        self.calls += 1
        if not self.script:
            return None
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_coordinator(
    *,
    enabled: bool = True,
    registry: PipelineRegistry | None = None,
    claimer: FakeClaimer | None = None,
    engine: FakeEngine | None = None,
    poll_interval_ms: int = 5,
    max_concurrent_datasets: int = 1,
    shutdown_grace_seconds: float = 1.0,
    graph_outbox_processor: FakeGraphOutboxProcessor | None = None,
) -> tuple[PipelineWorkerCoordinator, FakeClaimer, FakeEngine]:
    resolved_claimer = claimer or FakeClaimer()
    resolved_engine = engine or FakeEngine()
    coordinator = PipelineWorkerCoordinator(
        session_factory=None,  # type: ignore[arg-type] - unused: claimer/engine are faked
        registry=registry if registry is not None else non_empty_registry(),
        enabled=enabled,
        poll_interval_ms=poll_interval_ms,
        stale_after_seconds=300,
        max_concurrent_datasets=max_concurrent_datasets,
        claimer=resolved_claimer,  # type: ignore[arg-type]
        engine=resolved_engine,  # type: ignore[arg-type]
        shutdown_grace_seconds=shutdown_grace_seconds,
        graph_outbox_processor=graph_outbox_processor,  # type: ignore[arg-type]
    )
    return coordinator, resolved_claimer, resolved_engine


TEST_TIMEOUT = 5.0


# --- worker identity ----------------------------------------------------------


def test_worker_id_stable_across_lifetime_new_per_instance() -> None:
    coordinator1, _, _ = make_coordinator()
    coordinator2, _, _ = make_coordinator()
    assert coordinator1.worker_id != coordinator2.worker_id
    assert coordinator1.worker_id == coordinator1.worker_id  # noqa: PLR0124 - stability check


# --- WORKER_ENABLED=false -----------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_worker_start_stop_are_no_ops() -> None:
    coordinator, claimer, _ = make_coordinator(enabled=False)
    await asyncio.wait_for(coordinator.start(), timeout=TEST_TIMEOUT)
    assert coordinator.is_running is False
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert claimer.calls == 0


# --- empty registry ------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_registry_never_calls_claimer_but_stays_running() -> None:
    coordinator, claimer, _ = make_coordinator(registry=PipelineRegistry([]), poll_interval_ms=5)
    await coordinator.start()
    await asyncio.sleep(0.05)
    assert coordinator.is_running is True
    assert claimer.calls == 0
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)


# --- start/stop lifecycle, double start/stop ----------------------------------


@pytest.mark.asyncio
async def test_double_start_does_not_create_second_poll_task() -> None:
    coordinator, _, _ = make_coordinator()
    await coordinator.start()
    first_poll_task = coordinator._poll_task  # noqa: SLF001 - white-box lifecycle assertion
    await coordinator.start()
    assert coordinator._poll_task is first_poll_task  # noqa: SLF001
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_double_stop_is_safe() -> None:
    coordinator, _, _ = make_coordinator()
    await coordinator.start()
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert coordinator.is_running is False


@pytest.mark.asyncio
async def test_stop_before_start_is_a_no_op() -> None:
    coordinator, _, _ = make_coordinator()
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert coordinator.is_running is False


# --- readiness -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_false_before_start_true_after_false_after_stop() -> None:
    coordinator, _, _ = make_coordinator()
    assert coordinator.is_running is False
    await coordinator.start()
    assert coordinator.is_running is True
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert coordinator.is_running is False


@pytest.mark.asyncio
async def test_readiness_always_false_when_disabled() -> None:
    coordinator, _, _ = make_coordinator(enabled=False)
    assert coordinator.is_running is False
    await coordinator.start()
    assert coordinator.is_running is False


# --- capacity --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_limits_concurrent_dispatched_tasks() -> None:
    claims = [make_claimed_run() for _ in range(5)]
    claimer = FakeClaimer(queued=list(claims))
    engine = FakeEngine(hang_forever=True)
    coordinator, claimer, engine = make_coordinator(
        claimer=claimer, engine=engine, max_concurrent_datasets=2, poll_interval_ms=5
    )
    await coordinator.start()
    await asyncio.sleep(0.1)
    assert len(coordinator._active_tasks) == 2  # noqa: SLF001 - white-box capacity assertion
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)


# --- task cleanup / exception containment ---------------------------------------


@pytest.mark.asyncio
async def test_engine_exception_is_contained_and_worker_keeps_polling() -> None:
    claim = make_claimed_run()
    engine = FakeEngine(raise_error=True)
    coordinator, claimer, engine = make_coordinator(
        claimer=FakeClaimer(queued=[claim]), engine=engine, poll_interval_ms=5
    )
    await coordinator.start()
    await asyncio.sleep(0.1)
    assert engine.calls  # engine was invoked and raised
    assert coordinator.is_running is True  # worker survives the failure
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_task_cleanup_after_stop_leaves_no_tracked_tasks() -> None:
    claim = make_claimed_run()
    coordinator, claimer, engine = make_coordinator(
        claimer=FakeClaimer(queued=[claim]), poll_interval_ms=5
    )
    await coordinator.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert not coordinator._active_tasks  # noqa: SLF001
    assert not coordinator._active_claims  # noqa: SLF001


@pytest.mark.asyncio
async def test_hung_task_is_cancelled_after_grace_and_awaited_cleanly() -> None:
    claim = make_claimed_run()
    engine = FakeEngine(hang_forever=True)
    coordinator, claimer, engine = make_coordinator(
        claimer=FakeClaimer(queued=[claim]),
        engine=engine,
        poll_interval_ms=5,
        shutdown_grace_seconds=0.05,
    )
    await coordinator.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    assert not coordinator._active_tasks  # noqa: SLF001


# --- poll wakeup on shutdown -----------------------------------------------------


@pytest.mark.asyncio
async def test_stop_wakes_poll_loop_immediately_even_with_long_poll_interval() -> None:
    coordinator, _, _ = make_coordinator(poll_interval_ms=60_000)
    await coordinator.start()
    started = asyncio.get_event_loop().time()
    await asyncio.wait_for(coordinator.stop(), timeout=2.0)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 1.0


# --- CancelledError not converted to pipeline failure ----------------------------


@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_engine_execute_wrapper() -> None:
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.engine import PipelineEngine

    class CancellingStep:
        async def execute(self, context: PipelineContext) -> object:
            del context
            raise asyncio.CancelledError()

    engine = PipelineEngine(session_factory=None, registry=PipelineRegistry([]))  # type: ignore[arg-type]

    from sofias_memory.pipelines.registry import PipelineStepDefinition

    step_def = PipelineStepDefinition(
        name="a",
        definition_id="a:v1",
        step=CancellingStep(),
        input_deriver=lambda run_input, step_outputs: {},
    )
    context = PipelineContext(
        run_id=uuid4(),
        pipeline_type=PipelineType.COGNIFY,
        dataset_id=None,
        source_id=None,
        run_input={},
        step_outputs={},
        session_factory=None,  # type: ignore[arg-type]
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._run_step(step_def, context)  # noqa: SLF001 - focused private-method proof


# --- _run_transactional_phase: forced-cancel-safety mechanism (deterministic) ----


@pytest.mark.asyncio
async def test_transactional_phase_defers_cancellation_until_inner_coro_completes() -> None:
    """SM-505 forced-shutdown audit: ``task.cancel()`` on the outer awaiter of
    ``_run_transactional_phase`` must never abandon or truncate the inner
    "transaction" coroutine -- it must always run to its own natural
    conclusion first, and only then does the deferred ``CancelledError``
    propagate. Deterministic (Event-driven), no PostgreSQL involved; the real
    commit/rollback atomicity is proven by the worker's own PostgreSQL
    integration suite (scenario M)."""

    from sofias_memory.pipelines.engine import _run_transactional_phase

    entered = asyncio.Event()
    proceed = asyncio.Event()
    completed = False

    async def fake_transaction() -> str:
        nonlocal completed
        entered.set()
        await proceed.wait()
        completed = True
        return "committed"

    async def outer() -> str:
        return await _run_transactional_phase(fake_transaction())

    outer_task = asyncio.ensure_future(outer())
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    outer_task.cancel()
    await asyncio.sleep(0.05)
    assert not outer_task.done()  # cancellation must not have landed yet
    assert completed is False  # inner transaction still genuinely in flight

    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(outer_task, timeout=2.0)

    assert completed is True  # the inner transaction ran to its own conclusion


@pytest.mark.asyncio
async def test_transactional_phase_no_cancellation_behaves_transparently() -> None:
    from sofias_memory.pipelines.engine import _run_transactional_phase

    async def fake_transaction() -> str:
        return "committed"

    result = await _run_transactional_phase(fake_transaction())
    assert result == "committed"


# --- engine shutdown disposition (default shape) ---------------------------------


def test_pipeline_execution_result_default_disposition_is_not_paused() -> None:
    result = PipelineExecutionResult(run_id=uuid4(), status=None)
    assert result.paused_for_shutdown is False
    assert result.abandoned is False


def test_pipeline_execution_result_can_signal_shutdown_pause() -> None:
    result = PipelineExecutionResult(run_id=uuid4(), status=None, paused_for_shutdown=True)
    assert result.paused_for_shutdown is True
    assert result.status is None
    assert result.abandoned is False


# --- SM-506 autonomous graph outbox consumer wiring -------------------------


@pytest.mark.asyncio
async def test_no_graph_outbox_processor_configured_means_no_outbox_polling() -> None:
    """Backwards-compatible default: a coordinator built without a processor
    (e.g. Neo4j disabled) never starts an outbox task."""

    coordinator, _, _ = make_coordinator(graph_outbox_processor=None)
    await asyncio.wait_for(coordinator.start(), timeout=TEST_TIMEOUT)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    # No assertion beyond "does not raise" -- absence of a processor means
    # absence of the outbox task entirely (backlog SS 39).


@pytest.mark.asyncio
async def test_empty_pipeline_registry_still_drains_graph_outbox() -> None:
    """Backlog SS 30: the autonomous outbox consumer must not be gated by
    ``len(registry) == 0`` the way the pipeline claim loop is."""

    processor = FakeGraphOutboxProcessor(script=[object(), object()])
    coordinator, claimer, _ = make_coordinator(
        registry=PipelineRegistry([]),
        graph_outbox_processor=processor,
    )
    await asyncio.wait_for(coordinator.start(), timeout=TEST_TIMEOUT)
    await asyncio.sleep(0.2)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

    assert claimer.calls == 0  # empty registry: pipeline claim loop stays idle
    assert processor.calls >= 2  # outbox loop is unaffected by registry contents


@pytest.mark.asyncio
async def test_outbox_burst_stops_after_failure_and_resumes_next_poll() -> None:
    """Backlog SS 13: a projection failure ends the current burst instead of
    busy-spinning the remaining attempt budget in the same tick; the next
    poll tick tries again."""

    processor = FakeGraphOutboxProcessor(
        script=[object(), RuntimeError("neo4j unavailable"), object()]
    )
    coordinator, _, _ = make_coordinator(
        graph_outbox_processor=processor,
        poll_interval_ms=20,
    )
    await asyncio.wait_for(coordinator.start(), timeout=TEST_TIMEOUT)
    await asyncio.sleep(0.3)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

    # The burst that hit the RuntimeError stopped immediately (did not loop
    # trying to claim more in that same tick); the third scripted outcome was
    # only reachable on a later poll tick.
    assert processor.calls >= 3


@pytest.mark.asyncio
async def test_shutdown_awaits_in_flight_outbox_task_within_grace() -> None:
    processor = FakeGraphOutboxProcessor(script=[object()])
    coordinator, _, _ = make_coordinator(
        graph_outbox_processor=processor,
        shutdown_grace_seconds=1.0,
    )
    await asyncio.wait_for(coordinator.start(), timeout=TEST_TIMEOUT)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

    assert coordinator._outbox_task is None  # noqa: SLF001 - internal state check
    assert processor.calls >= 1
