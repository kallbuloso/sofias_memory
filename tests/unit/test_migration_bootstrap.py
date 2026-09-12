"""Unit tests for ADR-0015/SM-902's automatic serialized migration bootstrap
(``sofias_memory.services.migration_bootstrap``) -- exercised entirely
against fakes, independent of real PostgreSQL/Alembic/subprocess. Real-
PostgreSQL/real-subprocess proof lives in
``tests/integration/test_migration_bootstrap_postgres_integration.py``.
"""

from __future__ import annotations

from typing import cast

import pytest

from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.infrastructure.postgres.migration_state import (
    MigrationSchemaState,
    MigrationStateSnapshot,
)
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessResult
from sofias_memory.services.migration_bootstrap import (
    MigrationAction,
    MigrationBootstrap,
    MigrationClassificationFailedError,
    MigrationExecutionFailedError,
    MigrationLockTimeoutError,
    MigrationPostVerificationFailedError,
    MigrationSchemaInvalidError,
    migration_policy,
)

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"
VALID_NEO4J_PASSWORD = "fake-neo4j-password"
VALID_LLM_API_KEY = "sk-fake-test-key"

ALL_STATES = tuple(MigrationSchemaState)
MIGRATE_ELIGIBLE_STATES = (
    MigrationSchemaState.PRISTINE_FRESH_SCHEMA,
    MigrationSchemaState.KNOWN_ANCESTOR,
)
FAIL_CLOSED_IN_BOTH_MODES_STATES = tuple(
    state
    for state in ALL_STATES
    if state not in MIGRATE_ELIGIBLE_STATES and state is not MigrationSchemaState.EXACT_HEAD
)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "llm_api_key": VALID_LLM_API_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# migration_policy -- pure function, no I/O (Feature Contract v0.6.0 S5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", MIGRATE_ELIGIBLE_STATES)
def test_policy_migrates_eligible_states_under_auto(state: MigrationSchemaState) -> None:
    assert migration_policy(mode="auto", state=state) is MigrationAction.MIGRATE


@pytest.mark.parametrize("state", MIGRATE_ELIGIBLE_STATES)
def test_policy_fails_closed_for_eligible_states_under_verify_only(
    state: MigrationSchemaState,
) -> None:
    assert migration_policy(mode="verify_only", state=state) is MigrationAction.FAIL_CLOSED


def test_policy_no_ops_exact_head_under_both_modes() -> None:
    assert migration_policy(mode="auto", state=MigrationSchemaState.EXACT_HEAD) is (
        MigrationAction.NO_OP
    )
    assert migration_policy(mode="verify_only", state=MigrationSchemaState.EXACT_HEAD) is (
        MigrationAction.NO_OP
    )


@pytest.mark.parametrize("state", FAIL_CLOSED_IN_BOTH_MODES_STATES)
def test_policy_fails_closed_under_both_modes_for_non_eligible_states(
    state: MigrationSchemaState,
) -> None:
    assert migration_policy(mode="auto", state=state) is MigrationAction.FAIL_CLOSED
    assert migration_policy(mode="verify_only", state=state) is MigrationAction.FAIL_CLOSED


def test_policy_covers_exactly_the_nine_observable_states() -> None:
    assert len(ALL_STATES) == 9
    assert len(MIGRATE_ELIGIBLE_STATES) + len(FAIL_CLOSED_IN_BOTH_MODES_STATES) + 1 == 9


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeReadinessChecker:
    def __init__(self, *, results: list[PostgresReadinessResult] | None = None) -> None:
        self._results = list(results) if results is not None else [PostgresReadinessResult(True)]
        self.check_calls = 0

    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


class ExplodingReadinessChecker:
    async def check(self) -> PostgresReadinessResult:
        raise AssertionError("readiness must not be consulted in this scenario")


class FakeMigrationRunner:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[str] = []

    async def upgrade_head(self, *, database_url: str) -> int:
        self.calls.append(database_url)
        return self.exit_code


class ExplodingMigrationRunner:
    async def upgrade_head(self, *, database_url: str) -> int:
        raise AssertionError("alembic must not be invoked in this scenario")


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class FakeLockConnection:
    """Handles only the two SQL statements MigrationBootstrap's own advisory-
    lock code issues -- ``collect_migration_state_snapshot`` is monkeypatched
    out in these tests (SM-901's own snapshot/classification machinery has
    its own dedicated test file); this fake exists to prove SM-902's lock
    semantics specifically."""

    def __init__(
        self,
        *,
        lock_results: list[bool] | None = None,
        unlock_result: bool = True,
        raise_on_unlock: Exception | None = None,
    ) -> None:
        self.executed_sql: list[str] = []
        self.rollback_calls = 0
        self.commit_calls = 0
        self._lock_results = list(lock_results) if lock_results is not None else [True]
        self.unlock_result = unlock_result
        self._raise_on_unlock = raise_on_unlock

    async def execute(self, statement: object, params: object | None = None) -> _ScalarResult:
        sql = str(statement)
        self.executed_sql.append(sql)
        if "pg_try_advisory_lock" in sql:
            value = self._lock_results.pop(0) if self._lock_results else True
            return _ScalarResult(value)
        if "pg_advisory_unlock" in sql:
            if self._raise_on_unlock is not None:
                raise self._raise_on_unlock
            return _ScalarResult(self.unlock_result)
        raise AssertionError(f"unexpected SQL on the lock connection: {sql}")

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1


class _LockConnectionContext:
    def __init__(self, connection: FakeLockConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeLockConnection:
        return self._connection

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeLockEngine:
    def __init__(self, connection: FakeLockConnection) -> None:
        self.connection = connection
        self.connect_calls = 0
        self.dispose_calls = 0

    def connect(self) -> _LockConnectionContext:
        self.connect_calls += 1
        return _LockConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.dispose_calls += 1


def make_bootstrap(
    *,
    mode: str = "auto",
    readiness_checker: object,
    runner: object,
    lock_connection: FakeLockConnection | None = None,
    lock_acquire_deadline_seconds: float = 5.0,
    lock_acquire_poll_interval_seconds: float = 0.001,
) -> tuple[MigrationBootstrap, FakeLockEngine]:
    connection = lock_connection if lock_connection is not None else FakeLockConnection()
    engine = FakeLockEngine(connection)
    bootstrap = MigrationBootstrap(
        make_settings(database_migration_mode=cast(str, mode)),
        readiness_checker=cast(object, readiness_checker),  # type: ignore[arg-type]
        runner=cast(object, runner),  # type: ignore[arg-type]
        lock_engine_factory=lambda settings: cast(object, engine),  # type: ignore[arg-type,return-value]
        revision_graph_loader=lambda: cast(object, None),  # type: ignore[arg-type,return-value]
        code_heads_loader=lambda: frozenset({"0017"}),
        lock_acquire_deadline_seconds=lock_acquire_deadline_seconds,
        lock_acquire_poll_interval_seconds=lock_acquire_poll_interval_seconds,
    )
    return bootstrap, engine


def patch_classification(
    monkeypatch: pytest.MonkeyPatch, *, state: MigrationSchemaState, calls: list[str] | None = None
) -> None:
    async def fake_collect(
        connection: object, *, code_heads: frozenset[str]
    ) -> MigrationStateSnapshot:
        if calls is not None:
            calls.append("collect_migration_state_snapshot")
        return MigrationStateSnapshot(
            alembic_version_table_present=True,
            database_revisions=frozenset({"irrelevant"}),
            application_base_tables_present=True,
            code_heads=code_heads,
        )

    def fake_classify(snapshot: object, *, revision_graph: object) -> MigrationSchemaState:
        return state

    monkeypatch.setattr(
        "sofias_memory.services.migration_bootstrap.collect_migration_state_snapshot",
        fake_collect,
    )
    monkeypatch.setattr(
        "sofias_memory.services.migration_bootstrap.classify_migration_state", fake_classify
    )


# ---------------------------------------------------------------------------
# verify_only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_only_exact_head_delegates_to_readiness_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    runner = ExplodingMigrationRunner()
    bootstrap, engine = make_bootstrap(mode="verify_only", readiness_checker=checker, runner=runner)

    await bootstrap.ensure_schema_current()

    assert checker.check_calls == 1
    assert engine.connect_calls == 0
    assert engine.dispose_calls == 0


@pytest.mark.asyncio
async def test_verify_only_not_ready_fails_without_lock_or_subprocess() -> None:
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(False, failures=("x",))])
    runner = ExplodingMigrationRunner()
    bootstrap, engine = make_bootstrap(mode="verify_only", readiness_checker=checker, runner=runner)

    with pytest.raises(MigrationClassificationFailedError, match="schema not current"):
        await bootstrap.ensure_schema_current()

    assert checker.check_calls == 1
    assert engine.connect_calls == 0


# ---------------------------------------------------------------------------
# auto -- per-state action matrix (backlog SM-902 S29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_exact_head_locks_reclassifies_and_invokes_zero_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD, calls=calls)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    runner = ExplodingMigrationRunner()
    connection = FakeLockConnection(lock_results=[True])
    bootstrap, engine = make_bootstrap(
        mode="auto", readiness_checker=checker, runner=runner, lock_connection=connection
    )

    await bootstrap.ensure_schema_current()

    assert engine.connect_calls == 1
    assert connection.commit_calls == 1  # unlock committed
    assert checker.check_calls == 1
    assert calls == ["collect_migration_state_snapshot"]


@pytest.mark.parametrize("state", MIGRATE_ELIGIBLE_STATES)
@pytest.mark.asyncio
async def test_auto_migrate_eligible_states_invoke_exactly_one_upgrade(
    monkeypatch: pytest.MonkeyPatch, state: MigrationSchemaState
) -> None:
    patch_classification(monkeypatch, state=state)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    runner = FakeMigrationRunner(exit_code=0)
    bootstrap, engine = make_bootstrap(mode="auto", readiness_checker=checker, runner=runner)

    await bootstrap.ensure_schema_current()

    assert len(runner.calls) == 1
    assert runner.calls[0] == VALID_DATABASE_URL
    assert checker.check_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.parametrize("state", FAIL_CLOSED_IN_BOTH_MODES_STATES)
@pytest.mark.asyncio
async def test_auto_fail_closed_states_invoke_zero_upgrade(
    monkeypatch: pytest.MonkeyPatch, state: MigrationSchemaState
) -> None:
    patch_classification(monkeypatch, state=state)
    checker = ExplodingReadinessChecker()
    runner = ExplodingMigrationRunner()
    bootstrap, _engine = make_bootstrap(mode="auto", readiness_checker=checker, runner=runner)

    with pytest.raises(MigrationSchemaInvalidError, match=state.value):
        await bootstrap.ensure_schema_current()


# ---------------------------------------------------------------------------
# Lock semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_uses_session_level_try_lock_not_transaction_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    connection = FakeLockConnection(lock_results=[True])
    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=checker,
        runner=ExplodingMigrationRunner(),
        lock_connection=connection,
    )

    await bootstrap.ensure_schema_current()

    lock_sql = [sql for sql in connection.executed_sql if "advisory_lock" in sql]
    assert any("pg_try_advisory_lock(" in sql for sql in lock_sql)
    assert not any("xact" in sql for sql in connection.executed_sql)


@pytest.mark.asyncio
async def test_unlock_uses_pg_advisory_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    connection = FakeLockConnection(lock_results=[True])
    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=checker,
        runner=ExplodingMigrationRunner(),
        lock_connection=connection,
    )

    await bootstrap.ensure_schema_current()

    assert any("pg_advisory_unlock(" in sql for sql in connection.executed_sql)


@pytest.mark.asyncio
async def test_fresh_state_is_collected_after_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    connection = FakeLockConnection(lock_results=[False, False, True])
    original_execute = connection.execute

    async def recording_execute(statement: object, params: object | None = None) -> _ScalarResult:
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            calls.append("lock_attempt")
        return await original_execute(statement, params)

    connection.execute = recording_execute  # type: ignore[method-assign]

    patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD, calls=calls)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=checker,
        runner=ExplodingMigrationRunner(),
        lock_connection=connection,
        lock_acquire_poll_interval_seconds=0.0,
    )

    await bootstrap.ensure_schema_current()

    # Three lock attempts precede the single classification read -- state is
    # never observed until AFTER the lock is actually held.
    assert calls == [
        "lock_attempt",
        "lock_attempt",
        "lock_attempt",
        "collect_migration_state_snapshot",
    ]


@pytest.mark.asyncio
async def test_failed_lock_attempts_do_not_hold_an_open_transaction_during_sleep() -> None:
    connection = FakeLockConnection(lock_results=[False, False, True])
    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=FakeReadinessChecker(),
        runner=ExplodingMigrationRunner(),
        lock_connection=connection,
        lock_acquire_poll_interval_seconds=0.0,
    )

    acquired = await bootstrap._acquire_lock_with_deadline(connection)  # noqa: SLF001

    assert acquired is True
    # Every attempt (failed or not) is immediately followed by a rollback --
    # never left open while sleeping or while the subprocess later runs.
    assert connection.rollback_calls == 3


@pytest.mark.asyncio
async def test_no_transaction_remains_open_while_subprocess_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[int] = []
    connection = FakeLockConnection(lock_results=[True])

    async def fake_collect(conn: object, *, code_heads: frozenset[str]) -> MigrationStateSnapshot:
        return MigrationStateSnapshot(
            alembic_version_table_present=False,
            database_revisions=frozenset(),
            application_base_tables_present=False,
            code_heads=code_heads,
        )

    def fake_classify(snapshot: object, *, revision_graph: object) -> MigrationSchemaState:
        return MigrationSchemaState.PRISTINE_FRESH_SCHEMA

    monkeypatch.setattr(
        "sofias_memory.services.migration_bootstrap.collect_migration_state_snapshot", fake_collect
    )
    monkeypatch.setattr(
        "sofias_memory.services.migration_bootstrap.classify_migration_state", fake_classify
    )

    class RecordingRunner:
        async def upgrade_head(self, *, database_url: str) -> int:
            # By the time the subprocess runs, both the successful lock
            # attempt and the classification read have already been rolled
            # back -- no open transaction survives into this call.
            order.append(connection.rollback_calls)
            return 0

    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=FakeReadinessChecker(results=[PostgresReadinessResult(True)]),
        runner=RecordingRunner(),
        lock_connection=connection,
    )

    await bootstrap.ensure_schema_current()

    assert order == [2]


@pytest.mark.asyncio
async def test_lock_timeout_raises_without_invoking_subprocess() -> None:
    connection = FakeLockConnection(lock_results=[False, False, False, False, False])
    bootstrap, engine = make_bootstrap(
        mode="auto",
        readiness_checker=ExplodingReadinessChecker(),
        runner=ExplodingMigrationRunner(),
        lock_connection=connection,
        lock_acquire_deadline_seconds=0.01,
        lock_acquire_poll_interval_seconds=0.005,
    )

    with pytest.raises(MigrationLockTimeoutError):
        await bootstrap.ensure_schema_current()

    assert engine.dispose_calls == 1
    assert connection.rollback_calls >= 1


# ---------------------------------------------------------------------------
# Post-migration verification / failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_zero_with_not_ready_readiness_is_a_post_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.PRISTINE_FRESH_SCHEMA)
    checker = FakeReadinessChecker(results=[PostgresReadinessResult(False, failures=("x",))])
    runner = FakeMigrationRunner(exit_code=0)
    bootstrap, _engine = make_bootstrap(mode="auto", readiness_checker=checker, runner=runner)

    with pytest.raises(MigrationPostVerificationFailedError):
        await bootstrap.ensure_schema_current()

    assert len(runner.calls) == 1
    assert checker.check_calls == 1


@pytest.mark.asyncio
async def test_non_zero_exit_code_is_an_execution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.PRISTINE_FRESH_SCHEMA)
    checker = ExplodingReadinessChecker()
    runner = FakeMigrationRunner(exit_code=1)
    bootstrap, _engine = make_bootstrap(mode="auto", readiness_checker=checker, runner=runner)

    with pytest.raises(MigrationExecutionFailedError):
        await bootstrap.ensure_schema_current()

    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_execution_failure_is_not_retried_within_the_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``ensure_schema_current()`` call attempts Alembic at most once --
    the sticky "never retry within this process" rule is enforced one layer
    up, by ``lifespan._run_bootstrap`` catching
    ``MigrationExecutionFailedError`` specially (see
    ``test_lifespan_bootstrap.py``); this test only proves this class never
    loops internally."""

    patch_classification(monkeypatch, state=MigrationSchemaState.KNOWN_ANCESTOR)
    runner = FakeMigrationRunner(exit_code=1)
    bootstrap, _engine = make_bootstrap(
        mode="auto", readiness_checker=ExplodingReadinessChecker(), runner=runner
    )

    with pytest.raises(MigrationExecutionFailedError):
        await bootstrap.ensure_schema_current()

    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Corrective review Finding 1: a sticky failure must survive an unlock
# cleanup failure -- never masked by it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "exit_code", "readiness_result", "expected_error"),
    [
        (
            MigrationSchemaState.KNOWN_ANCESTOR,
            1,
            None,
            MigrationExecutionFailedError,
        ),
        (
            MigrationSchemaState.PRISTINE_FRESH_SCHEMA,
            0,
            PostgresReadinessResult(False, failures=("x",)),
            MigrationPostVerificationFailedError,
        ),
    ],
)
@pytest.mark.asyncio
async def test_sticky_failure_survives_an_unlock_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    state: MigrationSchemaState,
    exit_code: int,
    readiness_result: PostgresReadinessResult | None,
    expected_error: type[Exception],
) -> None:
    patch_classification(monkeypatch, state=state)
    checker = (
        ExplodingReadinessChecker()
        if readiness_result is None
        else FakeReadinessChecker(results=[readiness_result])
    )
    runner = FakeMigrationRunner(exit_code=exit_code)
    connection = FakeLockConnection(
        lock_results=[True], raise_on_unlock=RuntimeError("connection lost during cleanup")
    )
    bootstrap, _engine = make_bootstrap(
        mode="auto", readiness_checker=checker, runner=runner, lock_connection=connection
    )

    # The sticky error must be what's observable, never the cleanup
    # RuntimeError that occurred while releasing the lock afterward.
    with pytest.raises(expected_error):
        await bootstrap.ensure_schema_current()


# ---------------------------------------------------------------------------
# Corrective review Finding 2: raw readiness-checker exceptions become the
# correct sticky/non-sticky class depending on context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_head_readiness_exception_is_a_classification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD)

    class RaisingReadinessChecker:
        async def check(self) -> PostgresReadinessResult:
            raise RuntimeError("postgres exploded")

    bootstrap, _engine = make_bootstrap(
        mode="auto",
        readiness_checker=RaisingReadinessChecker(),
        runner=ExplodingMigrationRunner(),
    )

    with pytest.raises(MigrationClassificationFailedError):
        await bootstrap.ensure_schema_current()


@pytest.mark.asyncio
async def test_post_migration_readiness_exception_is_a_post_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_classification(monkeypatch, state=MigrationSchemaState.PRISTINE_FRESH_SCHEMA)

    class RaisingReadinessChecker:
        def __init__(self) -> None:
            self.check_calls = 0

        async def check(self) -> PostgresReadinessResult:
            self.check_calls += 1
            raise RuntimeError("postgres exploded")

    checker = RaisingReadinessChecker()
    runner = FakeMigrationRunner(exit_code=0)
    bootstrap, _engine = make_bootstrap(mode="auto", readiness_checker=checker, runner=runner)

    with pytest.raises(MigrationPostVerificationFailedError):
        await bootstrap.ensure_schema_current()

    assert len(runner.calls) == 1
    assert checker.check_calls == 1


# ---------------------------------------------------------------------------
# Corrective review Finding 3: migration_lock_waiting, once per attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_lock_waiting_is_logged_once_per_attempt_not_per_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from io import StringIO

    from sofias_memory.observability.logging import clear_log_context, configure_logging

    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    try:
        patch_classification(monkeypatch, state=MigrationSchemaState.EXACT_HEAD)
        checker = FakeReadinessChecker(results=[PostgresReadinessResult(True)])
        # Three failed pg_try_advisory_lock attempts before success -- the
        # event must appear exactly once, not three times.
        connection = FakeLockConnection(lock_results=[False, False, False, True])
        bootstrap, _engine = make_bootstrap(
            mode="auto",
            readiness_checker=checker,
            runner=ExplodingMigrationRunner(),
            lock_connection=connection,
            lock_acquire_poll_interval_seconds=0.0,
        )

        await bootstrap.ensure_schema_current()

        records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        waiting_events = [r for r in records if r.get("event") == "migration_lock_waiting"]
        assert len(waiting_events) == 1
    finally:
        clear_log_context()
