"""Real-PostgreSQL integration suite for SM-CTX-003 checkpoint supersession
(ADR-0018, Contract v1 Contextual Project Memory SS 31-34, handoff 11 SS 86-98).

Real PostgreSQL, the real ``ContextCheckpointService`` and the real HTTP app.
Concurrency is coordinated deterministically: a separate connection holds
``SELECT ... FOR UPDATE`` on the target while the contenders are started, and
the test only releases it after observing every contender *waiting on a lock*
in ``pg_stat_activity`` -- so the transactions genuinely overlap.

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus
``DATABASE_URL`` pointing at a dedicated, discardable database migrated to head
(0021) -- never a shared development database.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
from collections.abc import Sequence
from io import StringIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    ContextCheckpoint,
    ContextCheckpointProvenance,
    ContextCheckpointStatement,
    ContextualMemoryIdempotency,
)
from sofias_memory.infrastructure.postgres.repositories.context_checkpoint_statements import (
    ContextCheckpointStatementRepository,
)
from sofias_memory.infrastructure.postgres.repositories.context_checkpoints import (
    ContextCheckpointRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.observability.logging import clear_log_context, configure_logging
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointInvalidateRequest,
    ContextCheckpointResult,
    ContextCheckpointSupersedeRequest,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    BASE,
    HEADERS,
    HMAC_KEY,
    _test_database_url,
    assert_conflict,
    build_app,
    count,
    http,
    isolation_counts,
    key,
    keyed,
    ledger_count_for,
    new_scope,
    payload,
    request,
    service,
    session_factory,
)

__all__ = ["session_factory"]  # re-exported pytest fixture

CK = "feature:v1"


def snapshot(scope: str, **overrides: Any) -> dict[str, Any]:
    return payload(scope, **{"context_key": CK, **overrides})


def sup(scope: str, **overrides: Any) -> ContextCheckpointSupersedeRequest:
    return ContextCheckpointSupersedeRequest.model_validate(snapshot(scope, **overrides))


async def cleanup(factory: AsyncSessionFactory, scopes: Sequence[str]) -> None:
    """Lineage FK is ON DELETE RESTRICT: detach lineage (as tombstones, which
    satisfy every lifecycle CHECK) before deleting the test rows."""

    async with factory() as session:
        ids = list(
            (
                await session.scalars(
                    select(ContextCheckpoint.id).where(ContextCheckpoint.scope.in_(list(scopes)))
                )
            ).all()
        )
        if ids:
            await session.execute(
                text(
                    "DELETE FROM contextual_memory_idempotency WHERE result_resource_id = ANY(:i)"
                    " OR target_resource_id = ANY(:i)"
                ),
                {"i": ids},
            )
            await session.execute(
                text(
                    "UPDATE context_checkpoints SET lifecycle = 'forgotten', "
                    "forgotten_at = coalesce(forgotten_at, now()), invalidation_reason = NULL, "
                    "superseded_by = NULL, superseded_at = NULL WHERE id = ANY(:i)"
                ),
                {"i": ids},
            )
            await session.execute(
                text("DELETE FROM context_checkpoints WHERE id = ANY(:i)"), {"i": ids}
            )
        await session.commit()


async def rows(factory: AsyncSessionFactory, scope: str, ck: str = CK) -> list[ContextCheckpoint]:
    async with factory() as session:
        result = await session.scalars(
            select(ContextCheckpoint)
            .where(ContextCheckpoint.scope == scope, ContextCheckpoint.context_key == ck)
            .order_by(ContextCheckpoint.revision)
        )
        return list(result.all())


def shape(items: Sequence[ContextCheckpoint]) -> list[tuple[int, str]]:
    return [(item.revision, item.lifecycle.value) for item in items]


async def statement_count(factory: AsyncSessionFactory, checkpoint_id: UUID) -> int:
    return await count(
        factory,
        ContextCheckpointStatement,
        ContextCheckpointStatement.checkpoint_id == checkpoint_id,
    )


async def lock_waiters(probe: AsyncConnection) -> int:
    value = await probe.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
        )
    )
    await probe.rollback()
    return int(value or 0)


async def wait_for_waiters(probe: AsyncConnection, expected: int) -> None:
    for _ in range(300):
        if await lock_waiters(probe) >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {expected} lock waiters; contenders never overlapped")


async def run_queued(
    target: UUID, *coroutine_factories: Any
) -> list[ContextCheckpointResult | BaseException]:
    """Holds a row lock on ``target``, starts each contender in order and waits
    until it is observed blocked before starting the next (PostgreSQL grants
    row-lock waiters in queue order), then releases the holder."""

    engine = create_async_engine(_test_database_url(dict(os.environ)))
    try:
        async with engine.connect() as holder, engine.connect() as probe:
            await holder.execute(
                text("SELECT id FROM context_checkpoints WHERE id = :id FOR UPDATE"),
                {"id": target},
            )
            tasks = []
            for index, factory in enumerate(coroutine_factories, start=1):
                tasks.append(asyncio.ensure_future(factory()))
                await wait_for_waiters(probe, index)
            await holder.rollback()
        return list(await asyncio.gather(*tasks, return_exceptions=True))
    finally:
        await engine.dispose()


# --- normal supersession / lineage -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_linear_supersession_a_b_c_full_replacement_and_new_identities(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        b = await svc.supersede(
            a.checkpoint_id,
            sup(
                scope,
                objective=None,
                next_action=None,
                pending_work=[],
                approved_decisions=[{"text": "Episode será first-class."}],
                provenance={"origin_kind": "user_asserted", "source_system": "writer-b"},
            ),
            idempotency_key=key(),
        )
        assert (b.revision, b.lifecycle.value) == (2, "active")
        assert b.checkpoint_id != a.checkpoint_id
        # Full replacement: omitted/empty fields are NOT inherited.
        assert b.objective is None and b.next_action is None and b.pending_work == []
        assert [s.text for s in b.blockers] == [s.text for s in a.blockers]
        assert {s.statement_id for s in b.blockers}.isdisjoint({s.statement_id for s in a.blockers})
        assert b.provenance.source_system == "writer-b"
        assert b.provenance.conversation_ref is None
        # Same text, new statement identity.
        assert b.approved_decisions[0].text == a.approved_decisions[0].text
        assert b.approved_decisions[0].statement_id != a.approved_decisions[0].statement_id
        assert b.current_state is not None and a.current_state is not None
        assert b.current_state.statement_id != a.current_state.statement_id

        c = await svc.supersede(b.checkpoint_id, sup(scope), idempotency_key=key())
        assert c.revision == 3

        history = await rows(session_factory, scope)
        assert shape(history) == [(1, "superseded"), (2, "superseded"), (3, "active")]
        assert history[0].superseded_by == b.checkpoint_id
        assert history[1].superseded_by == c.checkpoint_id
        assert history[2].superseded_by is None and history[2].superseded_at is None
        assert all(r.superseded_at is not None for r in history[:2])
        # SUPERSEDED is valid history, not a tombstone.
        old = await svc.get(a.checkpoint_id)
        assert old.lifecycle.value == "superseded" and old.current_state == a.current_state
        assert old.pending_work == a.pending_work and old.provenance == a.provenance
        assert old.superseded_by == b.checkpoint_id
        assert await statement_count(session_factory, a.checkpoint_id) == 8
        assert (await svc.get_current(scope=scope, context_key=CK)).checkpoint_id == (
            c.checkpoint_id
        )
        listed = await svc.list_checkpoints(
            scope=scope,
            context_key=CK,
            lifecycle="superseded",  # type: ignore[arg-type]
            limit=50,
            cursor=None,
        )
        assert [i.revision for i in listed.items] == [2, 1]
        active = await svc.list_checkpoints(
            scope=scope, context_key=CK, lifecycle=None, limit=50, cursor=None
        )
        assert [i.checkpoint_id for i in active.items] == [c.checkpoint_id]
        provenance_rows = await count(
            session_factory,
            ContextCheckpointProvenance,
            ContextCheckpointProvenance.checkpoint_id.in_(
                [a.checkpoint_id, b.checkpoint_id, c.checkpoint_id]
            ),
        )
        assert provenance_rows == 3
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_supersede_stale_replay_and_conflict_details(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        async with http(build_app(session_factory)) as client:
            a = (await client.post(BASE, json=snapshot(scope), headers=keyed())).json()["data"]
            k1 = key("k1")
            body_b = snapshot(scope, current_state={"text": "B state"})
            b = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede", json=body_b, headers=keyed(k1)
            )
            assert b.status_code == 201, b.text
            b_data = b.json()["data"]
            assert b_data["revision"] == 2 and b_data["lifecycle"] == "active"

            stale = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede",
                json=snapshot(scope, current_state={"text": "C state"}),
                headers=keyed(),
            )
            assert stale.status_code == 409
            error = stale.json()["error"]
            assert error["code"] == "CONTEXT_STATE_CONFLICT"
            assert error["details"] == {
                "expected_checkpoint_id": a["checkpoint_id"],
                "current_checkpoint_id": b_data["checkpoint_id"],
            }

            replay = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede", json=body_b, headers=keyed(k1)
            )
            assert replay.status_code == 201 and replay.json()["data"] == b_data

            other_payload = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede",
                json=snapshot(scope, current_state={"text": "different"}),
                headers=keyed(k1),
            )
            assert other_payload.status_code == 409
            assert other_payload.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            missing = await client.post(
                f"{BASE}/{uuid4()}/supersede", json=snapshot(scope), headers=keyed()
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "CHECKPOINT_NOT_FOUND"

            no_state = snapshot(scope)
            del no_state["current_state"]
            rejected = await client.post(
                f"{BASE}/{b_data['checkpoint_id']}/supersede", json=no_state, headers=keyed()
            )
            assert rejected.status_code == 422

            current = await client.get(
                f"{BASE}/current", params={"scope": scope, "context_key": CK}, headers=HEADERS
            )
            assert current.json()["data"] == b_data

        assert shape(await rows(session_factory, scope)) == [(1, "superseded"), (2, "active")]
        assert await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(k1)) == 1
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_rejects_other_stream_or_project_and_non_active_targets(
    session_factory: AsyncSessionFactory,
) -> None:
    scope, other_scope = new_scope(), new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        for payload_scope, payload_key in (
            (other_scope, CK),
            (scope, "feature:other"),
            (scope, None),
        ):
            body = snapshot(payload_scope)
            if payload_key is None:
                del body["context_key"]  # resolves to 'main', never to the target's key
            else:
                body["context_key"] = payload_key
            with pytest.raises(SofiasMemoryError) as caught:
                await svc.supersede(
                    a.checkpoint_id,
                    ContextCheckpointSupersedeRequest.model_validate(body),
                    idempotency_key=key(),
                )
            assert_conflict(caught.value, "CONTEXT_STATE_CONFLICT")
        assert shape(await rows(session_factory, scope)) == [(1, "active")]
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == other_scope)
            == 0
        )

        await svc.invalidate(
            a.checkpoint_id, ContextCheckpointInvalidateRequest(reason="r"), idempotency_key=key()
        )
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        assert_conflict(caught.value, "CONTEXT_STATE_CONFLICT")
        assert caught.value.details["current_checkpoint_id"] is None
        await svc.forget(a.checkpoint_id, idempotency_key=key())
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        assert_conflict(caught.value, "CONTEXT_STATE_CONFLICT")
        assert shape(await rows(session_factory, scope)) == [(1, "forgotten")]
    finally:
        await cleanup(session_factory, [scope, other_scope])


# --- replay after later lifecycle changes -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_returns_original_replacement_after_it_is_superseded_or_forgotten(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        k1 = key("k1")
        body_b = sup(scope, current_state={"text": "B"})
        b = await svc.supersede(a.checkpoint_id, body_b, idempotency_key=k1)
        c = await svc.supersede(b.checkpoint_id, sup(scope), idempotency_key=key())

        replay_superseded = await svc.supersede(a.checkpoint_id, body_b, idempotency_key=k1)
        assert replay_superseded.checkpoint_id == b.checkpoint_id
        assert replay_superseded.lifecycle.value == "superseded"
        assert replay_superseded.superseded_by == c.checkpoint_id

        await svc.forget(b.checkpoint_id, idempotency_key=key())
        replay_forgotten = await svc.supersede(a.checkpoint_id, body_b, idempotency_key=k1)
        assert replay_forgotten.checkpoint_id == b.checkpoint_id
        assert replay_forgotten.lifecycle.value == "forgotten"
        assert replay_forgotten.current_state is None  # tombstone, never resurrected
        assert replay_forgotten.superseded_by == c.checkpoint_id  # structural lineage kept

        with pytest.raises(SofiasMemoryError) as caught:
            await svc.supersede(
                a.checkpoint_id, sup(scope, current_state={"text": "X"}), idempotency_key=k1
            )
        assert_conflict(caught.value, "IDEMPOTENCY_CONFLICT")
        assert shape(await rows(session_factory, scope)) == [
            (1, "superseded"),
            (2, "forgotten"),
            (3, "active"),
        ]
    finally:
        await cleanup(session_factory, [scope])


# --- lifecycle compatibility -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_invalidate_and_forget_preserve_lineage_and_never_cascade(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        b = await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        c = await svc.supersede(b.checkpoint_id, sup(scope), idempotency_key=key())

        inv_b = await svc.invalidate(
            b.checkpoint_id,
            ContextCheckpointInvalidateRequest(reason="SECRET-REASON"),
            idempotency_key=key(),
        )
        assert inv_b.lifecycle.value == "invalidated"
        assert inv_b.superseded_by == c.checkpoint_id and inv_b.superseded_at is not None
        assert inv_b.current_state is not None  # content preserved

        forgot_b = await svc.forget(b.checkpoint_id, idempotency_key=key())
        assert forgot_b.lifecycle.value == "forgotten"
        assert forgot_b.superseded_by == c.checkpoint_id and forgot_b.superseded_at is not None
        assert forgot_b.invalidated_at is not None and forgot_b.current_state is None
        assert forgot_b.provenance.conversation_ref is None and forgot_b.provenance.turn_ref is None

        forgot_a = await svc.forget(a.checkpoint_id, idempotency_key=key())
        assert forgot_a.superseded_by == b.checkpoint_id
        assert await statement_count(session_factory, a.checkpoint_id) == 0
        assert await statement_count(session_factory, b.checkpoint_id) == 0

        current = await svc.get_current(scope=scope, context_key=CK)
        assert current.checkpoint_id == c.checkpoint_id and current.lifecycle.value == "active"
        assert await statement_count(session_factory, c.checkpoint_id) == 8
        async with session_factory() as session:
            reason = await session.scalar(
                select(ContextCheckpoint.invalidation_reason).where(
                    ContextCheckpoint.id == b.checkpoint_id
                )
            )
        assert reason is None
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidating_or_forgetting_current_never_reactivates_superseded_history(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        b = await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        await svc.invalidate(
            b.checkpoint_id, ContextCheckpointInvalidateRequest(reason="r"), idempotency_key=key()
        )
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.get_current(scope=scope, context_key=CK)
        assert_conflict(caught.value, "CURRENT_CHECKPOINT_NOT_FOUND")
        assert shape(await rows(session_factory, scope)) == [(1, "superseded"), (2, "invalidated")]

        # Recovery create (SM-CTX-002) continues the revision sequence, no lineage.
        c = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        assert c.revision == 3
        d = await svc.supersede(c.checkpoint_id, sup(scope), idempotency_key=key())
        assert d.revision == 4
        await svc.forget(d.checkpoint_id, idempotency_key=key())
        with pytest.raises(SofiasMemoryError):
            await svc.get_current(scope=scope, context_key=CK)
        history = await rows(session_factory, scope)
        assert shape(history) == [
            (1, "superseded"),
            (2, "invalidated"),
            (3, "superseded"),
            (4, "forgotten"),
        ]
        assert history[1].superseded_by is None  # recovery create created no lineage
        assert history[0].superseded_by == b.checkpoint_id
    finally:
        await cleanup(session_factory, [scope])


# --- concurrency -------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_key_concurrent_supersede_has_one_winner_and_one_stale_conflict(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await run_queued(
            a.checkpoint_id,
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope, current_state={"text": "B"}), idempotency_key=key("k1")
            ),
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope, current_state={"text": "C"}), idempotency_key=key("k2")
            ),
        )
        winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert_conflict(losers[0], "CONTEXT_STATE_CONFLICT")
        assert isinstance(losers[0], SofiasMemoryError)
        assert losers[0].details["current_checkpoint_id"] == str(winners[0].checkpoint_id)
        assert winners[0].revision == 2
        history = await rows(session_factory, scope)
        assert shape(history) == [(1, "superseded"), (2, "active")]
        assert history[0].superseded_by == winners[0].checkpoint_id
        # The stale writer left nothing behind: no checkpoint, statements, ledger row.
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == scope) == 2
        )
        async with session_factory() as session:
            ledger = await session.scalar(
                select(func.count())
                .select_from(ContextualMemoryIdempotency)
                .where(
                    ContextualMemoryIdempotency.operation == "checkpoint_supersede",
                    ContextualMemoryIdempotency.target_resource_id == a.checkpoint_id,
                )
            )
        assert ledger == 1
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_concurrent_supersede_converges_to_one_replacement(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    idem = key("same")
    body = sup(scope, current_state={"text": "B"})
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await run_queued(
            a.checkpoint_id,
            lambda: service(session_factory).supersede(a.checkpoint_id, body, idempotency_key=idem),
            lambda: service(session_factory).supersede(a.checkpoint_id, body, idempotency_key=idem),
        )
        assert all(isinstance(r, ContextCheckpointResult) for r in results), results
        first, second = results
        assert isinstance(first, ContextCheckpointResult)
        assert isinstance(second, ContextCheckpointResult)
        assert first.checkpoint_id == second.checkpoint_id and first.revision == 2
        history = await rows(session_factory, scope)
        assert shape(history) == [(1, "superseded"), (2, "active")]
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 1
        )
        assert await statement_count(session_factory, first.checkpoint_id) == 8
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_different_payload_concurrently_is_idempotency_conflict(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    idem = key("same-diff")
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        results = await run_queued(
            a.checkpoint_id,
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope, current_state={"text": "P1"}), idempotency_key=idem
            ),
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope, current_state={"text": "P2"}), idempotency_key=idem
            ),
        )
        winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert_conflict(losers[0], "IDEMPOTENCY_CONFLICT")
        assert shape(await rows(session_factory, scope)) == [(1, "superseded"), (2, "active")]
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["invalidate", "forget"])
async def test_mutation_wins_then_supersede_is_stale(
    session_factory: AsyncSessionFactory, mutation: str
) -> None:
    scope = new_scope()
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )

        def mutate() -> Any:
            if mutation == "invalidate":
                return service(session_factory).invalidate(
                    a.checkpoint_id,
                    ContextCheckpointInvalidateRequest(reason="r"),
                    idempotency_key=key(),
                )
            return service(session_factory).forget(a.checkpoint_id, idempotency_key=key())

        results = await run_queued(
            a.checkpoint_id,
            mutate,
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope), idempotency_key=key()
            ),
        )
        assert isinstance(results[0], ContextCheckpointResult), results[0]
        assert_conflict(results[1], "CONTEXT_STATE_CONFLICT")
        expected = "invalidated" if mutation == "invalidate" else "forgotten"
        assert shape(await rows(session_factory, scope)) == [(1, expected)]
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["invalidate", "forget"])
async def test_supersede_wins_then_mutation_applies_to_history_only(
    session_factory: AsyncSessionFactory, mutation: str
) -> None:
    scope = new_scope()
    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )

        def mutate() -> Any:
            if mutation == "invalidate":
                return service(session_factory).invalidate(
                    a.checkpoint_id,
                    ContextCheckpointInvalidateRequest(reason="r"),
                    idempotency_key=key(),
                )
            return service(session_factory).forget(a.checkpoint_id, idempotency_key=key())

        results = await run_queued(
            a.checkpoint_id,
            lambda: service(session_factory).supersede(
                a.checkpoint_id, sup(scope), idempotency_key=key()
            ),
            mutate,
        )
        replacement, mutated = results
        assert isinstance(replacement, ContextCheckpointResult), replacement
        assert isinstance(mutated, ContextCheckpointResult), mutated
        expected = "invalidated" if mutation == "invalidate" else "forgotten"
        assert mutated.lifecycle.value == expected
        assert mutated.superseded_by == replacement.checkpoint_id
        history = await rows(session_factory, scope)
        assert shape(history) == [(1, expected), (2, "active")]
        assert history[0].superseded_by == replacement.checkpoint_id
        assert (
            await service(session_factory).get_current(scope=scope, context_key=CK)
        ).checkpoint_id == (replacement.checkpoint_id)
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_independent_streams_and_projects_are_not_blocked_by_a_locked_stream(
    session_factory: AsyncSessionFactory,
) -> None:
    scope, other_project = new_scope(), new_scope()
    svc = service(session_factory)
    try:
        locked = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        same_project = await svc.create_current(
            request(scope, context_key="feature:other"), idempotency_key=key()
        )
        foreign = await svc.create_current(
            request(other_project, context_key=CK), idempotency_key=key()
        )
        engine = create_async_engine(_test_database_url(dict(os.environ)))
        try:
            async with engine.connect() as holder:
                await holder.execute(
                    text("SELECT id FROM context_checkpoints WHERE id = :id FOR UPDATE"),
                    {"id": locked.checkpoint_id},
                )
                blocked = asyncio.ensure_future(
                    service(session_factory).supersede(
                        locked.checkpoint_id, sup(scope), idempotency_key=key()
                    )
                )
                b1 = await asyncio.wait_for(
                    svc.supersede(
                        same_project.checkpoint_id,
                        sup(scope, context_key="feature:other"),
                        idempotency_key=key(),
                    ),
                    timeout=10,
                )
                b2 = await asyncio.wait_for(
                    svc.supersede(foreign.checkpoint_id, sup(other_project), idempotency_key=key()),
                    timeout=10,
                )
                assert not blocked.done()
                assert b1.revision == 2 and b2.revision == 2
                await holder.rollback()
            done = await asyncio.wait_for(blocked, timeout=10)
            assert done.revision == 2
        finally:
            await engine.dispose()
    finally:
        await cleanup(session_factory, [scope, other_project])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mixed_concurrent_workload_never_deadlocks_or_breaks_invariants(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    streams = [f"mix:{i}" for i in range(4)]
    rng = random.Random(20261121)
    unexpected: list[BaseException] = []
    try:
        for ck in streams:
            await service(session_factory).create_current(
                request(scope, context_key=ck), idempotency_key=key()
            )

        async def one_operation(ck: str) -> None:
            svc = service(session_factory)
            try:
                choice = rng.choice(["supersede", "supersede", "invalidate", "forget", "create"])
                if choice == "create":
                    await svc.create_current(request(scope, context_key=ck), idempotency_key=key())
                    return
                current = await svc.get_current(scope=scope, context_key=ck)
                if choice == "supersede":
                    await svc.supersede(
                        current.checkpoint_id, sup(scope, context_key=ck), idempotency_key=key()
                    )
                elif choice == "invalidate":
                    await svc.invalidate(
                        current.checkpoint_id,
                        ContextCheckpointInvalidateRequest(reason="r"),
                        idempotency_key=key(),
                    )
                else:
                    await svc.forget(current.checkpoint_id, idempotency_key=key())
            except SofiasMemoryError as error:
                if error.code.value not in {
                    "CONTEXT_STATE_CONFLICT",
                    "CURRENT_CHECKPOINT_NOT_FOUND",
                    "CHECKPOINT_STATE_CONFLICT",
                }:
                    unexpected.append(error)
            except BaseException as error:  # noqa: BLE001 - collected for the assertion
                unexpected.append(error)

        for _ in range(15):
            await asyncio.wait_for(
                asyncio.gather(*[one_operation(rng.choice(streams)) for _ in range(8)]),
                timeout=60,
            )
        assert unexpected == []
        async with session_factory() as session:
            per_stream_active = (
                await session.execute(
                    text(
                        "SELECT context_key, count(*) FROM context_checkpoints WHERE scope = :s "
                        "AND lifecycle = 'active' GROUP BY context_key HAVING count(*) > 1"
                    ),
                    {"s": scope},
                )
            ).all()
            dup_lineage = await session.scalar(
                text(
                    "SELECT count(*) FROM (SELECT superseded_by FROM context_checkpoints WHERE "
                    "scope = :s AND superseded_by IS NOT NULL GROUP BY superseded_by "
                    "HAVING count(*) > 1) x"
                ),
                {"s": scope},
            )
            broken_lineage = await session.scalar(
                text(
                    "SELECT count(*) FROM context_checkpoints o JOIN context_checkpoints n "
                    "ON n.id = o.superseded_by WHERE o.scope = :s AND (n.scope <> o.scope OR "
                    "n.context_key <> o.context_key OR n.revision <> o.revision + 1)"
                ),
                {"s": scope},
            )
        assert per_stream_active == [] and dup_lineage == 0 and broken_lineage == 0
    finally:
        await cleanup(session_factory, [scope])


# --- rollback injection -------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failure_after_old_transition_rolls_everything_back(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    observed: dict[str, Any] = {}
    original_add = ContextCheckpointRepository.add

    async def failing_add(self: ContextCheckpointRepository, checkpoint: ContextCheckpoint) -> Any:
        if checkpoint.revision > 1:
            # The old transition is already flushed in this transaction.
            observed["old"] = await self._session.scalar(
                text("SELECT lifecycle::text FROM context_checkpoints WHERE id = :id"),
                {"id": observed["target"]},
            )
            raise RuntimeError("injected failure after old transition")
        return await original_add(self, checkpoint)

    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        observed["target"] = a.checkpoint_id
        monkeypatch.setattr(ContextCheckpointRepository, "add", failing_add)
        idem = key("fail-1")
        with pytest.raises(RuntimeError):
            await service(session_factory).supersede(
                a.checkpoint_id, sup(scope), idempotency_key=idem
            )
        monkeypatch.setattr(ContextCheckpointRepository, "add", original_add)
        assert observed["old"] == "superseded"
        history = await rows(session_factory, scope)
        assert shape(history) == [(1, "active")]
        assert history[0].superseded_by is None and history[0].superseded_at is None
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 0
        )
        # The key is reusable and the stream is intact.
        b = await service(session_factory).supersede(
            a.checkpoint_id, sup(scope), idempotency_key=idem
        )
        assert b.revision == 2
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failure_after_replacement_insert_rolls_everything_back(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    observed: dict[str, Any] = {}
    original_add_all = ContextCheckpointStatementRepository.add_all

    async def failing_add_all(self: ContextCheckpointStatementRepository, statements: Any) -> Any:
        inserted = await original_add_all(self, statements)
        if statements and statements[0].checkpoint_id != observed["target"]:
            observed["replacement_rows"] = await self._session.scalar(
                text("SELECT count(*) FROM context_checkpoints WHERE id = :id"),
                {"id": statements[0].checkpoint_id},
            )
            observed["replacement_id"] = statements[0].checkpoint_id
            raise RuntimeError("injected failure after replacement insert")
        return inserted

    try:
        a = await service(session_factory).create_current(
            request(scope, context_key=CK), idempotency_key=key()
        )
        observed["target"] = a.checkpoint_id
        monkeypatch.setattr(ContextCheckpointStatementRepository, "add_all", failing_add_all)
        with pytest.raises(RuntimeError):
            await service(session_factory).supersede(
                a.checkpoint_id, sup(scope), idempotency_key=key()
            )
        monkeypatch.setattr(ContextCheckpointStatementRepository, "add_all", original_add_all)
        assert observed["replacement_rows"] == 1
        replacement_id = observed["replacement_id"]
        assert shape(await rows(session_factory, scope)) == [(1, "active")]
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.id == replacement_id)
            == 0
        )
        assert await statement_count(session_factory, replacement_id) == 0
        assert (
            await count(
                session_factory,
                ContextCheckpointProvenance,
                ContextCheckpointProvenance.checkpoint_id == replacement_id,
            )
            == 0
        )
        assert (
            await count(
                session_factory,
                ContextualMemoryIdempotency,
                ContextualMemoryIdempotency.result_resource_id == replacement_id,
            )
            == 0
        )
    finally:
        await cleanup(session_factory, [scope])


# --- database protection --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_protects_linear_lineage_and_deferred_fk_is_checked_at_commit(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        b = await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        c = await svc.supersede(b.checkpoint_id, sup(scope), idempotency_key=key())

        async def violates(statements: Sequence[tuple[str, dict[str, Any]]]) -> None:
            async with session_factory() as session:
                with pytest.raises(DBAPIError):
                    for statement, params in statements:
                        await session.execute(text(statement), params)
                    await session.commit()
                await session.rollback()

        # Two predecessors for one replacement (A and B both -> C).
        await violates(
            [
                (
                    "UPDATE context_checkpoints SET superseded_by = :c WHERE id = :a",
                    {"a": a.checkpoint_id, "c": c.checkpoint_id},
                )
            ]
        )
        # Self supersession.
        await violates(
            [
                (
                    "UPDATE context_checkpoints SET superseded_by = id WHERE id = :a",
                    {"a": a.checkpoint_id},
                )
            ]
        )
        # A dangling lineage is rejected immediately by default ...
        await violates(
            [
                (
                    "UPDATE context_checkpoints SET superseded_by = :x WHERE id = :a",
                    {"a": a.checkpoint_id, "x": uuid4()},
                )
            ]
        )
        # ... and even when deferred, it can never be committed.
        await violates(
            [
                (
                    "SET CONSTRAINTS fk_context_checkpoints_superseded_by_context_checkpoints "
                    "DEFERRED",
                    {},
                ),
                (
                    "UPDATE context_checkpoints SET superseded_by = :x WHERE id = :a",
                    {"a": a.checkpoint_id, "x": uuid4()},
                ),
            ]
        )
        # SUPERSEDED without lineage, or a second ACTIVE, stay impossible.
        await violates(
            [
                (
                    "UPDATE context_checkpoints SET superseded_by = NULL, superseded_at = NULL "
                    "WHERE id = :a",
                    {"a": a.checkpoint_id},
                )
            ]
        )
        await violates(
            [
                (
                    "UPDATE context_checkpoints SET lifecycle = 'active', superseded_by = NULL, "
                    "superseded_at = NULL WHERE id = :b",
                    {"b": b.checkpoint_id},
                )
            ]
        )
        async with session_factory() as session:
            deferrable = (
                await session.execute(
                    text(
                        "SELECT condeferrable, condeferred FROM pg_constraint WHERE conname = "
                        "'fk_context_checkpoints_superseded_by_context_checkpoints'"
                    )
                )
            ).one()
        assert tuple(deferrable) == (True, False)  # deferrable, but immediate by default
    finally:
        await cleanup(session_factory, [scope])


# --- restart / isolation / privacy ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lineage_survives_restart_with_new_engine_and_process() -> None:
    _test_database_url(dict(os.environ))
    scope = new_scope()
    engine_one = create_async_engine_from_settings(load_settings())
    factory_one = create_session_factory(engine_one)
    try:
        svc = service(factory_one)
        a = await svc.create_current(request(scope, context_key=CK), idempotency_key=key())
        b = await svc.supersede(a.checkpoint_id, sup(scope), idempotency_key=key())
        c = await svc.supersede(b.checkpoint_id, sup(scope), idempotency_key=key())
    finally:
        await dispose_async_engine(engine_one)

    engine_two = create_async_engine_from_settings(load_settings())
    factory_two = create_session_factory(engine_two)
    try:
        history = await rows(factory_two, scope)
        assert shape(history) == [(1, "superseded"), (2, "superseded"), (3, "active")]
        assert [r.superseded_by for r in history] == [b.checkpoint_id, c.checkpoint_id, None]
        child = (
            "import asyncio\n"
            "from sofias_memory.config import load_settings\n"
            "from sofias_memory.infrastructure.postgres import create_async_engine_from_settings,"
            " create_session_factory, dispose_async_engine\n"
            "from sofias_memory.services.context_checkpoints import ContextCheckpointService\n"
            "async def main():\n"
            "    settings = load_settings()\n"
            "    engine = create_async_engine_from_settings(settings)\n"
            "    try:\n"
            "        svc = ContextCheckpointService(settings,"
            " session_factory=create_session_factory(engine))\n"
            f"        result = await svc.get_current(scope='{scope}', context_key='{CK}')\n"
            "        print(result.model_dump_json())\n"
            "    finally:\n"
            "        await dispose_async_engine(engine)\n"
            "asyncio.run(main())\n"
        )
        completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-authored script
            [sys.executable, "-c", child],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(os.environ),
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        current = json.loads(completed.stdout.strip().splitlines()[-1])
        assert current["checkpoint_id"] == str(c.checkpoint_id) and current["revision"] == 3
    finally:
        await cleanup(factory_two, [scope])
        await dispose_async_engine(engine_two)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersession_has_no_cross_feature_side_effects_and_logs_no_content(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    sentinel = f"SENTINEL-SUPERSEDE-{uuid4().hex}"
    keys = [f"SENTINEL-KEY-{i}-{uuid4().hex}" for i in range(3)]
    before = await isolation_counts(session_factory)
    stream = StringIO()
    clear_log_context()
    configure_logging("DEBUG", stream=stream)
    try:
        async with http(build_app(session_factory)) as client:
            a = (await client.post(BASE, json=snapshot(scope), headers=keyed(keys[0]))).json()[
                "data"
            ]
            body = snapshot(
                scope,
                current_state={"text": sentinel},
                provenance={
                    "origin_kind": "user_asserted",
                    "source_system": "s",
                    "conversation_ref": sentinel + "-ref",
                },
            )
            ok = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede", json=body, headers=keyed(keys[1])
            )
            assert ok.status_code == 201
            stale = await client.post(
                f"{BASE}/{a['checkpoint_id']}/supersede", json=body, headers=keyed(keys[2])
            )
            assert stale.status_code == 409
        logged = stream.getvalue()
        assert "checkpoint_superseded" in logged and "checkpoint_conflict" in logged
        for value in (sentinel, *keys, HMAC_KEY):
            assert value not in logged
        assert await isolation_counts(session_factory) == before
    finally:
        configure_logging("INFO")
        await cleanup(session_factory, [scope])
