"""Lock-order regression suite for checkpoint mutations (SM-CTX-003 review
finding: same Idempotency-Key reused concurrently across operations).

Every checkpoint mutation must acquire resources in one global order:

    idempotency key claim (ledger unique entry)  ->  checkpoint row lock

so no transaction ever holds a checkpoint row lock while waiting for a ledger
key held by another transaction -- the cycle that turned same-key
Invalidate/Forget x Supersede races into PostgreSQL deadlocks (HTTP 500).

Contention is deterministic: a holder connection keeps ``SELECT ... FOR UPDATE``
on the target; contenders are started one by one and each is observed blocked
on a lock in ``pg_stat_activity`` before the next starts; then the holder
releases. Every scenario runs under a bounded timeout.

Opt-in like the other contextual suites
(``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` + dedicated
``DATABASE_URL`` migrated to head).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import protect_contextual_idempotency_key
from sofias_memory.infrastructure.postgres.models import (
    ContextCheckpoint,
    ContextualMemoryIdempotency,
)
from sofias_memory.infrastructure.postgres.repositories.context_checkpoint_provenance import (
    ContextCheckpointProvenanceRepository,
)
from sofias_memory.infrastructure.postgres.repositories.context_checkpoint_statements import (
    ContextCheckpointStatementRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointInvalidateRequest,
    ContextCheckpointResult,
)
from tests.integration.test_context_checkpoint_supersession_postgres_integration import (
    CK,
    cleanup,
    rows,
    run_queued,
    shape,
    statement_count,
    sup,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    HMAC_KEY,
    assert_conflict,
    count,
    key,
    new_scope,
    request,
    service,
    session_factory,
)

__all__ = ["session_factory"]  # re-exported pytest fixture

SCENARIO_TIMEOUT_SECONDS = 30

type OperationFactory = Callable[[], Coroutine[Any, Any, ContextCheckpointResult]]


def operation(
    factory: AsyncSessionFactory, name: str, scope: str, target: UUID, idem: str
) -> OperationFactory:
    def start() -> Coroutine[Any, Any, ContextCheckpointResult]:
        svc = service(factory)
        if name == "supersede":
            return svc.supersede(target, sup(scope), idempotency_key=idem)
        if name == "invalidate":
            return svc.invalidate(
                target, ContextCheckpointInvalidateRequest(reason="r"), idempotency_key=idem
            )
        if name == "forget":
            return svc.forget(target, idempotency_key=idem)
        raise AssertionError(name)

    return start


async def contended(target: UUID, *factories: OperationFactory) -> list[Any]:
    return await asyncio.wait_for(run_queued(target, *factories), timeout=SCENARIO_TIMEOUT_SECONDS)


def assert_no_database_error(results: list[Any]) -> None:
    for result in results:
        assert not isinstance(result, DBAPIError), f"database error (deadlock?): {result!r}"
        assert not (
            isinstance(result, BaseException) and not isinstance(result, SofiasMemoryError)
        ), repr(result)


async def ledger_rows(factory: AsyncSessionFactory, idem: str) -> list[tuple[str, UUID | None]]:
    protected = protect_contextual_idempotency_key(secret=HMAC_KEY.encode(), idempotency_key=idem)
    async with factory() as session:
        result = await session.execute(
            select(
                ContextualMemoryIdempotency.operation,
                ContextualMemoryIdempotency.result_resource_id,
            ).where(ContextualMemoryIdempotency.idempotency_key == protected)
        )
        return [(op.value, rid) for op, rid in result.all()]


EXPECTED_STATE = {
    "supersede": [(1, "superseded"), (2, "active")],
    "invalidate": [(1, "invalidated")],
    "forget": [(1, "forgotten")],
}
OPERATION_VALUE = {
    "supersede": "checkpoint_supersede",
    "invalidate": "checkpoint_invalidate",
    "forget": "checkpoint_forget",
}


# --- Cases A/B/C: different operations, SAME key -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("invalidate", "supersede"),  # Case A, the original deadlock order
        ("supersede", "invalidate"),  # Case A, reverse order
        ("forget", "supersede"),  # Case B
        ("supersede", "forget"),  # Case B, reverse order
        ("invalidate", "forget"),  # Case C
        ("forget", "invalidate"),  # Case C, reverse order
    ],
)
async def test_same_key_different_operations_never_deadlock(
    session_factory: AsyncSessionFactory, first: str, second: str
) -> None:
    scope = new_scope()
    idem = key("shared")
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await contended(
            a.checkpoint_id,
            operation(session_factory, first, scope, a.checkpoint_id, idem),
            operation(session_factory, second, scope, a.checkpoint_id, idem),
        )
        assert_no_database_error(results)
        winner, loser = results
        assert isinstance(winner, ContextCheckpointResult), winner
        assert_conflict(loser, "IDEMPOTENCY_CONFLICT")
        assert shape(await rows(session_factory, scope)) == EXPECTED_STATE[first]
        ledger = await ledger_rows(session_factory, idem)
        assert ledger == [(OPERATION_VALUE[first], winner.checkpoint_id)]
    finally:
        await cleanup(session_factory, [scope])


# --- Case D: same operation, same key -> replay -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["supersede", "invalidate", "forget"])
async def test_same_key_same_operation_concurrently_replays(
    session_factory: AsyncSessionFactory, name: str
) -> None:
    scope = new_scope()
    idem = key("same-op")
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        body = sup(scope)

        def start() -> Coroutine[Any, Any, ContextCheckpointResult]:
            svc = service(session_factory)
            if name == "supersede":
                return svc.supersede(a.checkpoint_id, body, idempotency_key=idem)
            return operation(session_factory, name, scope, a.checkpoint_id, idem)()

        results = await contended(a.checkpoint_id, start, start)
        assert_no_database_error(results)
        first, second = results
        assert isinstance(first, ContextCheckpointResult), first
        assert isinstance(second, ContextCheckpointResult), second
        assert first == second
        assert shape(await rows(session_factory, scope)) == EXPECTED_STATE[name]
        assert len(await ledger_rows(session_factory, idem)) == 1
    finally:
        await cleanup(session_factory, [scope])


# --- Case E: different keys keep the approved race semantics ---------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "second_outcome", "state"),
    [
        ("invalidate", "forget", "ok", [(1, "forgotten")]),
        ("forget", "invalidate", "CHECKPOINT_STATE_CONFLICT", [(1, "forgotten")]),
        ("invalidate", "supersede", "CONTEXT_STATE_CONFLICT", [(1, "invalidated")]),
        ("supersede", "invalidate", "ok", [(1, "invalidated"), (2, "active")]),
        ("forget", "supersede", "CONTEXT_STATE_CONFLICT", [(1, "forgotten")]),
        ("supersede", "forget", "ok", [(1, "forgotten"), (2, "active")]),
    ],
)
async def test_different_keys_keep_serialized_race_semantics(
    session_factory: AsyncSessionFactory,
    first: str,
    second: str,
    second_outcome: str,
    state: list[tuple[int, str]],
) -> None:
    scope = new_scope()
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await contended(
            a.checkpoint_id,
            operation(session_factory, first, scope, a.checkpoint_id, key("k1")),
            operation(session_factory, second, scope, a.checkpoint_id, key("k2")),
        )
        assert_no_database_error(results)
        assert isinstance(results[0], ContextCheckpointResult), results[0]
        if second_outcome == "ok":
            assert isinstance(results[1], ContextCheckpointResult), results[1]
        else:
            assert_conflict(results[1], second_outcome)
        history = await rows(session_factory, scope)
        assert shape(history) == state
        if len(history) == 2:
            assert history[0].superseded_by == history[1].id
    finally:
        await cleanup(session_factory, [scope])


# --- Case G: Create shares the ledger; same key with a concurrent Supersede ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_and_supersede_with_same_key_do_not_deadlock(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    idem = key("create-vs-supersede")
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await contended(
            a.checkpoint_id,
            # Supersede claims the key, then waits for the target row lock.
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope), idempotency_key=idem
            ),
            # Create on another (empty) stream takes its advisory lock, then
            # waits on the same ledger key.
            lambda: service(session_factory).create_current(
                request(scope, context_key="feature:new"), idempotency_key=idem
            ),
        )
        assert_no_database_error(results)
        superseded, created = results
        assert isinstance(superseded, ContextCheckpointResult), superseded
        assert_conflict(created, "IDEMPOTENCY_CONFLICT")
        assert shape(await rows(session_factory, scope)) == EXPECTED_STATE["supersede"]
        assert await rows(session_factory, scope, "feature:new") == []
        assert await ledger_rows(session_factory, idem) == [
            ("checkpoint_supersede", superseded.checkpoint_id)
        ]
    finally:
        await cleanup(session_factory, [scope])


# --- Case F: failure injection rolls back lifecycle + claim (invalidate/forget) ----------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_failure_after_statement_delete_rolls_everything_back(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    idem = key("forget-fail")
    original = ContextCheckpointStatementRepository.delete_by_checkpoint_id

    async def failing(self: ContextCheckpointStatementRepository, checkpoint_id: UUID) -> None:
        await original(self, checkpoint_id)
        raise RuntimeError("injected failure after statement delete")

    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        monkeypatch.setattr(
            ContextCheckpointStatementRepository, "delete_by_checkpoint_id", failing
        )
        with pytest.raises(RuntimeError):
            await service(session_factory).forget(a.checkpoint_id, idempotency_key=idem)
        monkeypatch.setattr(
            ContextCheckpointStatementRepository, "delete_by_checkpoint_id", original
        )
        assert shape(await rows(session_factory, scope)) == [(1, "active")]
        assert await statement_count(session_factory, a.checkpoint_id) == 8
        assert await ledger_rows(session_factory, idem) == []
        forgotten = await service(session_factory).forget(a.checkpoint_id, idempotency_key=idem)
        assert forgotten.lifecycle.value == "forgotten"
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_failure_after_transition_rolls_everything_back(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    idem = key("invalidate-fail")
    original = ContextCheckpointProvenanceRepository.get_by_checkpoint_ids

    async def failing(self: ContextCheckpointProvenanceRepository, ids: Any) -> Any:
        raise RuntimeError("injected failure after lifecycle transition flush")

    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        monkeypatch.setattr(ContextCheckpointProvenanceRepository, "get_by_checkpoint_ids", failing)
        with pytest.raises(RuntimeError):
            await service(session_factory).invalidate(
                a.checkpoint_id,
                ContextCheckpointInvalidateRequest(reason="r"),
                idempotency_key=idem,
            )
        monkeypatch.setattr(
            ContextCheckpointProvenanceRepository, "get_by_checkpoint_ids", original
        )
        history = await rows(session_factory, scope)
        assert shape(history) == [(1, "active")]
        assert history[0].invalidated_at is None and history[0].invalidation_reason is None
        assert await ledger_rows(session_factory, idem) == []
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == scope) == 1
        )
    finally:
        await cleanup(session_factory, [scope])
