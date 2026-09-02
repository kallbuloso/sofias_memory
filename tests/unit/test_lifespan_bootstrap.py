"""Unit tests for the ADR-0011 D7/D31/D33/D43 bootstrap sequence
(STORAGE-007) -- ``_attempt_bootstrap``/``_run_convergence_to_fixed_point``/
``_run_bootstrap`` exercised directly against fakes, independent of FastAPI/
TestClient/real PostgreSQL/Neo4j/S3."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest

from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.readiness import (
    PostgresReadinessChecker,
    PostgresReadinessResult,
)
from sofias_memory.infrastructure.storage import SourceStorageRouter
from sofias_memory.lifespan import (
    BOOTSTRAP_RETRY_INTERVAL_SECONDS,
    CONVERGENCE_POLL_INTERVAL_SECONDS,
    _attempt_bootstrap,
    _run_bootstrap,
    _run_convergence_to_fixed_point,
)
from sofias_memory.observability.logging import get_logger
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder
from sofias_memory.services.storage_convergence import (
    CaseBLineage,
    ConvergenceResult,
    IntegrityFailure,
    IntegrityFailureReason,
    StorageConvergenceService,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg, arg-type]


class _FakeReadinessChecker:
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready
        self.check_calls = 0

    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        return PostgresReadinessResult(ready=self._ready, failures=() if self._ready else ("x",))


class _FakeNeo4jResource:
    def __init__(self) -> None:
        self.bootstrapped = False


class _FakeConvergenceService:
    def __init__(self, results: list[ConvergenceResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def converge(self) -> ConvergenceResult:
        self.calls += 1
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


async def _noop_bootstrap_neo4j(resource: object) -> None:
    cast(_FakeNeo4jResource, resource).bootstrapped = True


async def _noop_probe_postgres(session_factory: object) -> None:
    del session_factory


def _fake_session_factory() -> object:
    return object()


# ---------------------------------------------------------------------------
# Schema gate (D32)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_not_ready_blocks_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._bootstrap_neo4j", _noop_bootstrap_neo4j)
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _noop_probe_postgres)

    holder = ProcessStateHolder()
    checker = _FakeReadinessChecker(ready=False)

    with pytest.raises(RuntimeError, match="schema not current"):
        await _attempt_bootstrap(
            settings=make_settings(),
            holder=holder,
            session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
            postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
            neo4j_resource=None,
            recovery=None,
            worker=None,
            source_storage_router=None,
            convergence_service=None,
            logger=get_logger(__name__),
        )

    assert holder.state is ProcessState.BOOTSTRAP_MAINTENANCE


@pytest.mark.asyncio
async def test_filesystem_backend_reaches_operational_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._bootstrap_neo4j", _noop_bootstrap_neo4j)
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _noop_probe_postgres)

    holder = ProcessStateHolder()
    checker = _FakeReadinessChecker(ready=True)

    await _attempt_bootstrap(
        settings=make_settings(storage_backend="filesystem"),
        holder=holder,
        session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
        postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
        neo4j_resource=None,
        recovery=None,
        worker=None,
        source_storage_router=None,
        convergence_service=None,
        logger=get_logger(__name__),
    )

    assert holder.state is ProcessState.OPERATIONAL


@pytest.mark.asyncio
async def test_s3_backend_enters_storage_converging_before_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._bootstrap_neo4j", _noop_bootstrap_neo4j)
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _noop_probe_postgres)

    holder = ProcessStateHolder()
    checker = _FakeReadinessChecker(ready=True)
    observed_states: list[ProcessState] = []

    class _ObservingConvergence:
        async def converge(self) -> ConvergenceResult:
            observed_states.append(holder.state)
            return ConvergenceResult()

    await _attempt_bootstrap(
        settings=make_settings(
            storage_backend="s3", storage_s3_bucket="b", storage_s3_region="us-east-1"
        ),
        holder=holder,
        session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
        postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
        neo4j_resource=None,
        recovery=None,
        worker=None,
        source_storage_router=None,
        convergence_service=cast(StorageConvergenceService, _ObservingConvergence()),
        logger=get_logger(__name__),
    )

    assert observed_states == [ProcessState.STORAGE_CONVERGING]
    assert holder.state is ProcessState.OPERATIONAL


@pytest.mark.asyncio
async def test_entering_storage_converging_starts_recovery_owned_set_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowing set left over from an earlier, abandoned bootstrap attempt
    (this same holder, retried by ``_run_bootstrap``) must never carry
    forward into a fresh attempt as if it were still current."""

    monkeypatch.setattr("sofias_memory.lifespan._bootstrap_neo4j", _noop_bootstrap_neo4j)
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _noop_probe_postgres)

    holder = ProcessStateHolder()
    holder.set_recovery_owned_run_ids([uuid4()])  # stale, from a prior attempt
    checker = _FakeReadinessChecker(ready=True)
    observed_at_first_converge: frozenset[object] | None = None

    class _ObservingConvergence:
        async def converge(self) -> ConvergenceResult:
            nonlocal observed_at_first_converge
            if observed_at_first_converge is None:
                observed_at_first_converge = holder.recovery_owned_run_ids
            return ConvergenceResult()

    await _attempt_bootstrap(
        settings=make_settings(
            storage_backend="s3", storage_s3_bucket="b", storage_s3_region="us-east-1"
        ),
        holder=holder,
        session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
        postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
        neo4j_resource=None,
        recovery=None,
        worker=None,
        source_storage_router=None,
        convergence_service=cast(StorageConvergenceService, _ObservingConvergence()),
        logger=get_logger(__name__),
    )

    assert observed_at_first_converge == frozenset()


@pytest.mark.asyncio
async def test_s3_probe_failure_keeps_storage_converging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._bootstrap_neo4j", _noop_bootstrap_neo4j)
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _noop_probe_postgres)

    holder = ProcessStateHolder()
    checker = _FakeReadinessChecker(ready=True)

    class _FailingProbeRouter:
        async def probe(self) -> None:
            raise RuntimeError("s3 unreachable")

    with pytest.raises(RuntimeError, match="s3 unreachable"):
        await _attempt_bootstrap(
            settings=make_settings(
                storage_backend="s3", storage_s3_bucket="b", storage_s3_region="us-east-1"
            ),
            holder=holder,
            session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
            postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
            neo4j_resource=None,
            recovery=None,
            worker=None,
            source_storage_router=cast(SourceStorageRouter, _FailingProbeRouter()),
            convergence_service=None,
            logger=get_logger(__name__),
        )

    assert holder.state is ProcessState.STORAGE_CONVERGING


# ---------------------------------------------------------------------------
# Fixed-point convergence loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convergence_integrity_failure_retries_until_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("sofias_memory.lifespan.asyncio.sleep", fake_sleep)

    failing = ConvergenceResult(
        integrity_failures=(
            IntegrityFailure(
                source_id=uuid4(),
                dataset_id=uuid4(),
                reason=IntegrityFailureReason.LOCAL_OBJECT_MISSING,
                message="missing",
            ),
        )
    )
    clean = ConvergenceResult()
    service = _FakeConvergenceService([failing, clean])

    holder = ProcessStateHolder()
    await _run_convergence_to_fixed_point(
        cast(StorageConvergenceService, service), holder, get_logger(__name__)
    )

    assert service.calls == 2
    assert sleeps == [CONVERGENCE_POLL_INTERVAL_SECONDS]


@pytest.mark.asyncio
async def test_convergence_waits_for_non_terminal_recovery_owned_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sofias_memory.domain import PipelineRunStatus, PipelineType

    async def fake_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("sofias_memory.lifespan.asyncio.sleep", fake_sleep)

    pending_lineage = ConvergenceResult(
        recovery_owned_case_b=(
            CaseBLineage(
                source_id=uuid4(),
                dataset_id=uuid4(),
                pipeline_run_id=uuid4(),
                pipeline_type=PipelineType.FORGET,
                pipeline_run_status=PipelineRunStatus.RUNNING,
            ),
        )
    )
    terminal_lineage = ConvergenceResult(
        recovery_owned_case_b=(
            CaseBLineage(
                source_id=uuid4(),
                dataset_id=uuid4(),
                pipeline_run_id=uuid4(),
                pipeline_type=PipelineType.FORGET,
                pipeline_run_status=PipelineRunStatus.SUCCEEDED,
            ),
        )
    )
    service = _FakeConvergenceService([pending_lineage, terminal_lineage])

    holder = ProcessStateHolder()
    await _run_convergence_to_fixed_point(
        cast(StorageConvergenceService, service), holder, get_logger(__name__)
    )

    assert service.calls == 2
    # Final fail-closed audit: published wholesale from the LATEST
    # converge() call only, never merged/accumulated across passes -- the
    # first pass's (now-stale) pending lineage id must not linger.
    assert holder.recovery_owned_run_ids == {
        terminal_lineage.recovery_owned_case_b[0].pipeline_run_id
    }


@pytest.mark.asyncio
async def test_convergence_legitimate_failed_lineage_does_not_block_fixed_point() -> None:
    from sofias_memory.domain import PipelineRunStatus, PipelineType

    failed_but_terminal = ConvergenceResult(
        recovery_owned_case_b=(
            CaseBLineage(
                source_id=uuid4(),
                dataset_id=uuid4(),
                pipeline_run_id=uuid4(),
                pipeline_type=PipelineType.DATASET_DELETE,
                pipeline_run_status=PipelineRunStatus.FAILED,
            ),
        )
    )
    service = _FakeConvergenceService([failed_but_terminal])

    holder = ProcessStateHolder()
    await _run_convergence_to_fixed_point(
        cast(StorageConvergenceService, service), holder, get_logger(__name__)
    )

    assert service.calls == 1
    assert holder.recovery_owned_run_ids == {
        failed_but_terminal.recovery_owned_case_b[0].pipeline_run_id
    }


# ---------------------------------------------------------------------------
# Outer retry loop -- never crash-loops, never silently reports OPERATIONAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_retries_on_unexpected_exception_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan.BOOTSTRAP_RETRY_INTERVAL_SECONDS", 0.0)
    holder = ProcessStateHolder()
    attempts = 0

    async def flaky_attempt(**kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")
        cast(ProcessStateHolder, kwargs["holder"]).transition(ProcessState.OPERATIONAL)

    monkeypatch.setattr("sofias_memory.lifespan._attempt_bootstrap", flaky_attempt)

    await asyncio.wait_for(
        _run_bootstrap(
            settings=make_settings(),
            holder=holder,
            session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
            postgres_readiness_checker=None,
            neo4j_resource=None,
            recovery=None,
            worker=None,
            source_storage_router=None,
            convergence_service=None,
        ),
        timeout=5.0,
    )

    assert attempts == 3
    assert holder.state is ProcessState.OPERATIONAL


@pytest.mark.asyncio
async def test_bootstrap_cancellation_propagates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan.BOOTSTRAP_RETRY_INTERVAL_SECONDS", 1000.0)
    holder = ProcessStateHolder()

    async def always_fails(**kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("sofias_memory.lifespan._attempt_bootstrap", always_fails)

    task = asyncio.create_task(
        _run_bootstrap(
            settings=make_settings(),
            holder=holder,
            session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
            postgres_readiness_checker=None,
            neo4j_resource=None,
            recovery=None,
            worker=None,
            source_storage_router=None,
            convergence_service=None,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert holder.state is ProcessState.BOOTSTRAP_MAINTENANCE


def test_bootstrap_retry_interval_is_bounded() -> None:
    assert 0 < BOOTSTRAP_RETRY_INTERVAL_SECONDS <= 60


@pytest.mark.asyncio
async def test_unexpected_bootstrap_defect_is_logged_distinctly_and_never_becomes_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected bootstrap defect audit: a genuine programming defect (here,
    a ``TypeError`` -- never an ordinary dependency-unavailable condition) is
    logged with its own exception type as a ``bootstrap_attempt_failed``
    event and never converted into ``OPERATIONAL`` -- the maintenance
    surface stays alive and NOT_READY, but the specific failure is neither
    silently flattened into an ordinary S3/schema-unavailable condition nor
    hidden."""

    import json
    from io import StringIO

    from sofias_memory.observability.logging import clear_log_context, configure_logging

    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)

    monkeypatch.setattr("sofias_memory.lifespan.BOOTSTRAP_RETRY_INTERVAL_SECONDS", 1000.0)
    holder = ProcessStateHolder()

    async def defective_attempt(**kwargs: object) -> None:
        raise TypeError("a genuine programming defect, not a dependency failure")

    monkeypatch.setattr("sofias_memory.lifespan._attempt_bootstrap", defective_attempt)

    task = asyncio.create_task(
        _run_bootstrap(
            settings=make_settings(),
            holder=holder,
            session_factory=cast(object, _fake_session_factory()),  # type: ignore[arg-type]
            postgres_readiness_checker=None,
            neo4j_resource=None,
            recovery=None,
            worker=None,
            source_storage_router=None,
            convergence_service=None,
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert holder.state is ProcessState.BOOTSTRAP_MAINTENANCE

        records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        failure_records = [r for r in records if r.get("event") == "bootstrap_attempt_failed"]
        assert failure_records
        assert failure_records[0]["exception_type"] == "TypeError"
        assert failure_records[0]["process_state"] == ProcessState.BOOTSTRAP_MAINTENANCE.value
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        clear_log_context()
