"""Real-PostgreSQL integration suite for SM-CTX-005 provenance-aware Forget
propagation over explicit Evidence (handoff 13 SS 108-136, AC-005.x).

Direct (synchronous) Forget surfaces: Cognitive Memory precise Forget and
Episode direct Forget, plus statement/checkpoint convergence, corruption
fail-closed, replay/incremental convergence and deterministic concurrency
(holder connections keep a lock while contenders are observed waiting in
``pg_stat_activity``; timeouts are bounded). Knowledge pipeline surfaces live in
``test_contextual_forget_knowledge_postgres_integration.py``.

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a dedicated
``DATABASE_URL`` migrated to head (0022).
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import EvidenceKey, EvidenceSourceType, context_stream_lock_key
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    ContextCheckpointStatement,
    ContextualEvidenceRef,
    ContextualMemoryIdempotency,
    EpisodeEvidenceRef,
    GraphOutbox,
    PipelineRun,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.episodes import EpisodeInvalidateRequest
from sofias_memory.services import contextual_evidence_invalidator as invalidator_module
from sofias_memory.services.contextual_evidence import EvidenceResolver, transient_sqlstate
from sofias_memory.services.contextual_evidence_invalidator import (
    ContextualEvidenceInvalidator,
    PropagationCounts,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    _test_database_url,
    assert_conflict,
    count,
    key,
    ledger_count_for,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    payload as checkpoint_payload,
)
from tests.integration.test_contextual_evidence_postgres_integration import (
    availability_of,
    checkpoints,
    cleanup,
    create_memory,
    episode_request,
    episodes,
    lock_waiters,
    memories,
    ref,
    run_with_row_held,
    seed,
    session_factory,
    support,
)

__all__ = ["session_factory"]

T = EvidenceSourceType


# --- helpers -----------------------------------------------------------------------------


async def new_episode(
    factory: AsyncSessionFactory,
    scope: str,
    sup: dict[str, Any] | None = None,
    *,
    episode_id: UUID | None = None,
) -> UUID:
    overrides: dict[str, Any] = {"support": sup} if sup is not None else {}
    kwargs: dict[str, Any] = {"episode_id": episode_id} if episode_id is not None else {}
    result = await episodes(factory).create(
        episode_request(scope, **overrides), idempotency_key=key(), **kwargs
    )
    return result.episode_id


def statement(text_value: str, sup: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text_value}
    if sup is not None:
        body["support"] = sup
    return body


def checkpoint_body(
    scope: str,
    context_key: str,
    *,
    current_state: dict[str, Any],
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return checkpoint_payload(
        scope,
        context_key=context_key,
        current_state=current_state,
        pending_work=pending if pending is not None else [],
    )


async def create_checkpoint(factory: AsyncSessionFactory, body: dict[str, Any]) -> Any:
    return await checkpoints(factory).create_current(
        ContextCheckpointCreateRequest.model_validate(body), idempotency_key=key()
    )


def assert_tombstone(result: Any) -> None:
    assert result.lifecycle.value == "forgotten"
    for field in ("scope", "context_key", "kind", "summary", "situation", "reason", "outcome"):
        assert getattr(result, field) is None, field
    assert result.occurred_at is None and result.confidence is None
    assert result.support is None
    assert result.provenance.conversation_ref is None and result.provenance.turn_ref is None


def assert_intact(result: Any) -> None:
    assert result.lifecycle.value == "active"
    assert result.summary is not None and result.scope is not None


async def links_of(factory: AsyncSessionFactory, episode_id: UUID) -> int:
    return await count(factory, EpisodeEvidenceRef, EpisodeEvidenceRef.episode_id == episode_id)


async def canonical_exists(factory: AsyncSessionFactory, source_type: T, source_id: UUID) -> bool:
    return (
        await count(
            factory,
            ContextualEvidenceRef,
            ContextualEvidenceRef.source_type == source_type,
            ContextualEvidenceRef.source_id == source_id,
        )
        == 1
    )


async def ordinals(factory: AsyncSessionFactory, checkpoint_id: UUID) -> dict[UUID, int]:
    async with factory() as s:
        rows = await s.execute(
            select(ContextCheckpointStatement.id, ContextCheckpointStatement.ordinal).where(
                ContextCheckpointStatement.checkpoint_id == checkpoint_id
            )
        )
        return {sid: ordinal for sid, ordinal in rows.all()}


async def hold_and_run(
    sql: str, params: dict[str, Any], *starters: Callable[[], Awaitable[Any]]
) -> list[Any]:
    """A holder connection runs ``sql`` (taking a lock) while each contender is
    started and observed blocked; then the holder releases (bounded waits)."""

    engine = create_async_engine(_test_database_url(dict(os.environ)))
    try:
        async with engine.connect() as holder, engine.connect() as probe:
            await holder.execute(text(sql), params)
            tasks = []
            for n, start in enumerate(starters, start=1):
                tasks.append(asyncio.ensure_future(start()))
                for _ in range(500):
                    if await lock_waiters(probe) >= n:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError(f"contender {n} never blocked")
            await holder.rollback()
        return list(await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 60))
    finally:
        await engine.dispose()


def no_op_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates the incremental (pre-SM-CTX-005) period: Forget without propagation."""

    async def skip(self: Any, seeds: Any, **kwargs: Any) -> PropagationCounts:
        return PropagationCounts()

    monkeypatch.setattr(ContextualEvidenceInvalidator, "propagate", skip)


# --- direct Cognitive Memory Forget (AC-005.2/6/10/22) ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cognitive_forget_propagates_transitively_in_the_same_commit(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    m = seeds.memory
    try:
        a = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("memory_item", m))
        )
        c = await new_episode(
            session_factory,
            seeds.scope,
            support(
                "any_sufficient", ref("memory_item", m), ref("session_entry", seeds.session_entry)
            ),
        )
        d = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        p = await new_episode(session_factory, seeds.scope)  # provenance only, support null
        body = checkpoint_body(
            seeds.scope,
            "t1",
            current_state=statement(
                "estado", support("any_sufficient", ref("session_entry", seeds.session_entry))
            ),
            pending=[
                statement("x depende de A", support("all_required", ref("episode", a))),
                statement(
                    "y independente",
                    support("all_required", ref("session_entry", seeds.session_entry)),
                ),
            ],
        )
        cp = await create_checkpoint(session_factory, body)
        y_id = cp.pending_work[1].statement_id
        p_before = await eps.get(p)
        counts_before = {
            "runs": await count(session_factory, PipelineRun),
            "outbox": await count(session_factory, GraphOutbox),
            "ctx_ledger": await count(session_factory, ContextualMemoryIdempotency),
            "cog_ledger": await count(session_factory, CognitiveMemoryIdempotency),
        }

        forgotten = await memories(session_factory).forget(m, idempotency_key=key("fm"))

        assert forgotten.lifecycle.value == "forgotten"
        assert await availability_of(session_factory, T.MEMORY_ITEM, m) == "forgotten"
        assert_tombstone(await eps.get(a))
        assert_tombstone(await eps.get(d))  # transitive through Episode A
        survivor = await eps.get(c)
        assert_intact(survivor)
        assert survivor.support is not None
        assert {e.source_type.value: e.availability.value for e in survivor.support.evidence} == {
            "memory_item": "forgotten",
            "session_entry": "available",
        }
        assert await eps.get(p) == p_before  # no provenance-only cascade
        after = await checkpoints(session_factory).get(cp.checkpoint_id)
        assert after.lifecycle.value == "active"
        assert [s.statement_id for s in after.pending_work] == [y_id]
        assert (await ordinals(session_factory, cp.checkpoint_id))[y_id] == 1  # no renumbering
        # Canonical identities survive; scrubbed owners release outgoing links.
        assert await canonical_exists(session_factory, T.MEMORY_ITEM, m)
        assert await canonical_exists(session_factory, T.EPISODE, a)
        assert await links_of(session_factory, a) == 0 and await links_of(session_factory, d) == 0
        assert await links_of(session_factory, c) == 2
        # No derived PipelineRun, graph event or per-dependent ledger entry.
        assert await count(session_factory, PipelineRun) == counts_before["runs"]
        assert await count(session_factory, GraphOutbox) == counts_before["outbox"]
        assert (
            await count(session_factory, ContextualMemoryIdempotency) == counts_before["ctx_ledger"]
        )
        assert (
            await count(session_factory, CognitiveMemoryIdempotency)
            == counts_before["cog_ledger"] + 1
        )
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cognitive_forget_rolls_back_entirely_when_propagation_fails(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("memory_item", seeds.memory))
        )
        ledger_before = await count(session_factory, CognitiveMemoryIdempotency)

        async def boom(self: Any, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("injected propagation failure")

        monkeypatch.setattr(ContextualEvidenceInvalidator, "_is_sufficient", boom)
        idem = key("fm-rollback")
        with pytest.raises(RuntimeError):
            await memories(session_factory).forget(seeds.memory, idempotency_key=idem)
        assert await availability_of(session_factory, T.MEMORY_ITEM, seeds.memory) == "available"
        assert_intact(await eps.get(a))
        assert await count(session_factory, CognitiveMemoryIdempotency) == ledger_before

        monkeypatch.undo()
        await memories(session_factory).forget(seeds.memory, idempotency_key=idem)
        assert_tombstone(await eps.get(a))
    finally:
        await cleanup(session_factory, seeds)


# --- direct Episode Forget (AC-005.3/23) ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_forget_cascade_is_atomic_with_rollback_on_partial_failure(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        c = await new_episode(
            session_factory,
            seeds.scope,
            support("any_sufficient", ref("episode", b), ref("session_entry", seeds.session_entry)),
        )
        d = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", b))
        )

        original = invalidator_module.scrub_episode
        calls = {"n": 0}

        def failing_scrub(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # B already scrubbed and flushed, D fails
                raise RuntimeError("injected cascade failure")
            original(*args, **kwargs)

        monkeypatch.setattr(invalidator_module, "scrub_episode", failing_scrub)
        idem = key("fe")
        with pytest.raises(RuntimeError):
            await eps.forget(a, idempotency_key=idem)
        for episode_id in (a, b, c, d):
            assert_intact(await eps.get(episode_id))
        assert await links_of(session_factory, b) == 1
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 0
        )

        monkeypatch.undo()
        result = await eps.forget(a, idempotency_key=idem)
        assert_tombstone(result)
        assert_tombstone(await eps.get(b))
        assert_tombstone(await eps.get(d))
        assert_intact(await eps.get(c))  # still supported by the SessionEntry
        assert await canonical_exists(session_factory, T.EPISODE, a)
        assert await canonical_exists(session_factory, T.EPISODE, b)
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_required_cascades_while_any_sufficient_preserves(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        both = (ref("episode", a), ref("memory_item", seeds.memory))
        b_any = await new_episode(session_factory, seeds.scope, support("any_sufficient", *both))
        b_all = await new_episode(session_factory, seeds.scope, support("all_required", *both))
        before = await eps.get(b_any)

        await eps.forget(a, idempotency_key=key())

        kept = await eps.get(b_any)
        assert_intact(kept)
        assert kept.summary == before.summary and kept.situation == before.situation
        assert kept.support is not None
        assert {e.source_type.value: e.availability.value for e in kept.support.evidence} == {
            "episode": "forgotten",
            "memory_item": "available",
        }
        assert_tombstone(await eps.get(b_all))
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_hop_and_shared_ancestors_converge_on_latest_state(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    m = seeds.memory
    try:
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("memory_item", m))
        )
        c = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", b))
        )
        d = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", c))
        )
        # E is reachable from both B and C; both are lost in the same Forget, so
        # E's final decision must see both (Critical Gate 3).
        e = await new_episode(
            session_factory,
            seeds.scope,
            support("any_sufficient", ref("episode", b), ref("episode", c)),
        )
        f = await new_episode(
            session_factory,
            seeds.scope,
            support("any_sufficient", ref("episode", c), ref("session_entry", seeds.session_entry)),
        )
        other = await new_episode(session_factory, seeds.scope)
        g = await new_episode(
            session_factory,
            seeds.scope,
            support("all_required", ref("episode", d), ref("episode", other)),
        )

        await memories(session_factory).forget(m, idempotency_key=None)

        for scrubbed in (b, c, d, e, g):
            assert_tombstone(await eps.get(scrubbed))
        assert_intact(await eps.get(f))
        assert_intact(await eps.get(other))
    finally:
        await cleanup(session_factory, seeds)


# --- ContextStatements and checkpoints (AC-005.7/8/9/25/26) ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_statement_scrub_preserves_survivors_then_current_state_loss_invalidates(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    cps = checkpoints(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        m2 = await create_memory(session_factory, seeds.scope)
        body = checkpoint_body(
            seeds.scope,
            "t6",
            current_state=statement("estado", support("all_required", ref("memory_item", m2))),
            pending=[
                statement("x", support("all_required", ref("episode", a))),
                statement("y", support("all_required", ref("memory_item", m2))),
                statement("z", support("all_required", ref("episode", a))),
            ],
        )
        cp = await create_checkpoint(session_factory, body)
        y_id = cp.pending_work[1].statement_id
        current_id = cp.current_state.statement_id

        await episodes(session_factory).forget(a, idempotency_key=key())

        partial = await cps.get(cp.checkpoint_id)
        assert partial.lifecycle.value == "active"
        assert [s.statement_id for s in partial.pending_work] == [y_id]
        assert (
            partial.current_state is not None and partial.current_state.statement_id == current_id
        )
        assert (await ordinals(session_factory, cp.checkpoint_id))[y_id] == 1
        assert (
            await cps.get_current(scope=seeds.scope, context_key="t6")
        ).checkpoint_id == cp.checkpoint_id

        await memories(session_factory).forget(m2, idempotency_key=None)

        invalidated = await cps.get(cp.checkpoint_id)
        assert invalidated.lifecycle.value == "invalidated"
        assert invalidated.current_state is None and invalidated.pending_work == []
        assert invalidated.invalidated_at is not None
        assert invalidated.objective is not None and invalidated.next_action is not None
        with pytest.raises(SofiasMemoryError) as caught:
            await cps.get_current(scope=seeds.scope, context_key="t6")
        assert_conflict(caught.value, "CURRENT_CHECKPOINT_NOT_FOUND")
        listed = await cps.list_checkpoints(
            scope=seeds.scope,
            context_key="t6",
            lifecycle=invalidated.lifecycle,
            limit=10,
            cursor=None,
        )
        assert [item.checkpoint_id for item in listed.items] == [cp.checkpoint_id]
        assert listed.items[0].current_state is None

        # Recovery is a new Create (MAX + 1), never a fallback.
        fresh = await create_checkpoint(
            session_factory, checkpoint_body(seeds.scope, "t6", current_state=statement("novo"))
        )
        assert fresh.revision == 2 and fresh.lifecycle.value == "active"
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_historical_fallback_and_superseded_invalidation_keeps_lineage(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    cps = checkpoints(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(session_factory, seeds.scope)
        rev1 = await create_checkpoint(
            session_factory,
            checkpoint_body(
                seeds.scope,
                "t7",
                current_state=statement("r1", support("all_required", ref("episode", a))),
            ),
        )
        rev2 = await cps.supersede(
            rev1.checkpoint_id,
            ContextCheckpointSupersedeRequest.model_validate(
                checkpoint_body(
                    seeds.scope,
                    "t7",
                    current_state=statement("r2", support("all_required", ref("episode", b))),
                )
            ),
            idempotency_key=key(),
        )

        await eps.forget(b, idempotency_key=key())

        r2 = await cps.get(rev2.checkpoint_id)
        assert r2.lifecycle.value == "invalidated" and r2.current_state is None
        r1 = await cps.get(rev1.checkpoint_id)
        assert r1.lifecycle.value == "superseded" and r1.superseded_by == rev2.checkpoint_id
        with pytest.raises(SofiasMemoryError) as caught:
            await cps.get_current(scope=seeds.scope, context_key="t7")
        assert_conflict(caught.value, "CURRENT_CHECKPOINT_NOT_FOUND")

        await eps.forget(a, idempotency_key=key())

        r1_after = await cps.get(rev1.checkpoint_id)
        assert r1_after.lifecycle.value == "invalidated" and r1_after.current_state is None
        assert r1_after.superseded_by == rev2.checkpoint_id
        assert r1_after.superseded_at == r1.superseded_at
        assert (await cps.get(rev2.checkpoint_id)).lifecycle.value == "invalidated"
    finally:
        await cleanup(session_factory, seeds)


# --- non-triggers (AC-005.16/17, handoff 13 SS 14/64/122) ----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_session_archive_and_ordinary_invalidation_never_cascade(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        provenance_only = await new_episode(session_factory, seeds.scope)
        from_entry = await new_episode(
            session_factory,
            seeds.scope,
            support("all_required", ref("session_entry", seeds.session_entry)),
        )
        a = await new_episode(session_factory, seeds.scope)
        on_invalidated = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        snapshot = {
            eid: await eps.get(eid) for eid in (provenance_only, from_entry, on_invalidated)
        }

        async with session_factory() as s:
            await s.execute(
                text("UPDATE sessions SET status = 'archived', archived_at = now() WHERE id = :i"),
                {"i": seeds.session_id},
            )
            await s.commit()
        await eps.invalidate(a, EpisodeInvalidateRequest(reason="r"), idempotency_key=key())
        await memories(session_factory).forget(seeds.memory, idempotency_key=None)

        assert await eps.get(provenance_only) == snapshot[provenance_only]
        assert await eps.get(from_entry) == snapshot[from_entry]
        kept = await eps.get(on_invalidated)
        assert_intact(kept)  # unsupported at read time, but invalidation is no trigger
        assert kept.summary == snapshot[on_invalidated].summary
        assert (
            kept.support is not None
            and kept.support.evidence[0].availability.value == "unavailable"
        )
    finally:
        await cleanup(session_factory, seeds)


# --- corruption fails closed (AC-005.24) -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corrupt_cycle_and_inconsistent_support_fail_closed(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        c = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", b))
        )
        async with session_factory() as s:  # bypass the API: B -> C closes a cycle B -> C -> B
            await s.execute(
                text(
                    "INSERT INTO contextual_evidence_refs (id, source_type, source_id) "
                    "VALUES (gen_random_uuid(), 'episode', :c) ON CONFLICT DO NOTHING"
                ),
                {"c": c},
            )
            await s.execute(
                text(
                    "INSERT INTO episode_evidence_refs (episode_id, evidence_ref_id) "
                    "SELECT :b, r.id FROM contextual_evidence_refs r "
                    "WHERE r.source_type = 'episode' AND r.source_id = :c"
                ),
                {"b": b, "c": c},
            )
            await s.commit()
        idem = key("cycle")
        with pytest.raises(SofiasMemoryError) as caught:
            await eps.forget(a, idempotency_key=idem)
        assert caught.value.code.value == "INTERNAL_ERROR"
        assert "episode" not in caught.value.message.lower()
        for episode_id in (a, b, c):
            assert_intact(await eps.get(episode_id))
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 0
        )
        async with session_factory() as s:
            await s.execute(
                text(
                    "DELETE FROM episode_evidence_refs l USING contextual_evidence_refs r "
                    "WHERE l.episode_id = :b AND l.evidence_ref_id = r.id AND r.source_id = :c"
                ),
                {"b": b, "c": c},
            )
            await s.commit()

        # Statement with links but no support_mode: indeterminate -> fail closed.
        cp = await create_checkpoint(
            session_factory,
            checkpoint_body(
                seeds.scope,
                "t10",
                current_state=statement("estado"),
                pending=[statement("x", support("all_required", ref("episode", a)))],
            ),
        )
        x_id = cp.pending_work[0].statement_id
        async with session_factory() as s:
            await s.execute(
                text("UPDATE context_checkpoint_statements SET support_mode = NULL WHERE id = :i"),
                {"i": x_id},
            )
            await s.commit()
        with pytest.raises(SofiasMemoryError) as caught:
            await eps.forget(a, idempotency_key=key())
        assert caught.value.code.value == "INTERNAL_ERROR"
        assert_intact(await eps.get(a))
        async with session_factory() as s:
            await s.execute(
                text(
                    "UPDATE context_checkpoint_statements SET support_mode = 'all_required' "
                    "WHERE id = :i"
                ),
                {"i": x_id},
            )
            await s.commit()
        await eps.forget(a, idempotency_key=key())
        assert_tombstone(await eps.get(c))
    finally:
        await cleanup(session_factory, seeds)


# --- replay / incremental convergence / idempotency (AC-005.27, SS 76-78) ------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_and_new_key_forget_converge_incremental_era_dependents(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        a2 = await new_episode(session_factory, seeds.scope)
        b2 = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a2))
        )
        m3 = await create_memory(session_factory, seeds.scope)
        b3 = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("memory_item", m3))
        )
        k_a, k_m3 = key("replay-a"), key("replay-m3")

        no_op_propagation(monkeypatch)
        await eps.forget(a, idempotency_key=k_a)
        await eps.forget(a2, idempotency_key=key())
        await memories(session_factory).forget(m3, idempotency_key=k_m3)
        for left_behind in (b, b2, b3):
            assert_intact(await eps.get(left_behind))  # incremental-era inconsistency
        monkeypatch.undo()

        replay = await eps.forget(a, idempotency_key=k_a)  # same-key replay
        assert_tombstone(replay)
        assert_tombstone(await eps.get(b))
        assert await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(k_a)) == 1

        await eps.forget(a2, idempotency_key=key())  # new key, target already FORGOTTEN
        assert_tombstone(await eps.get(b2))

        await memories(session_factory).forget(m3, idempotency_key=k_m3)  # Cognitive replay
        assert_tombstone(await eps.get(b3))

        # Re-running the invalidator over the same loss mutates nothing more.
        async with PostgresUnitOfWork(session_factory) as uow:
            again = await ContextualEvidenceInvalidator(uow).propagate(
                [
                    EvidenceKey(T.EPISODE, a),
                    EvidenceKey(T.EPISODE, a2),
                    EvidenceKey(T.MEMORY_ITEM, m3),
                ]
            )
            await uow.commit()
        assert again.episodes_forgotten == 0
        assert again.statements_removed == 0 and again.checkpoints_invalidated == 0
    finally:
        await cleanup(session_factory, seeds)


# --- restart non-resurrection (handoff 13 SS 133) ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_process_reads_keep_tombstones_removed_statements_and_no_fallback(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        rev1 = await create_checkpoint(
            session_factory, checkpoint_body(seeds.scope, "t133", current_state=statement("r1"))
        )
        rev2 = await checkpoints(session_factory).supersede(
            rev1.checkpoint_id,
            ContextCheckpointSupersedeRequest.model_validate(
                checkpoint_body(
                    seeds.scope,
                    "t133",
                    current_state=statement("r2", support("all_required", ref("episode", b))),
                    pending=[statement("p", support("all_required", ref("episode", a)))],
                )
            ),
            idempotency_key=key(),
        )
        await episodes(session_factory).forget(a, idempotency_key=key())

        # A brand-new engine/session factory/services: nothing in-process survives.
        from sofias_memory.infrastructure.postgres import create_session_factory

        engine = create_async_engine(_test_database_url(dict(os.environ)))
        try:
            fresh = create_session_factory(engine)
            eps2, cps2 = episodes(fresh), checkpoints(fresh)
            assert_tombstone(await eps2.get(a))
            assert_tombstone(await eps2.get(b))
            r2 = await cps2.get(rev2.checkpoint_id)
            assert r2.lifecycle.value == "invalidated"
            assert r2.current_state is None and r2.pending_work == []
            assert (await cps2.get(rev1.checkpoint_id)).lifecycle.value == "superseded"
            with pytest.raises(SofiasMemoryError) as caught:
                await cps2.get_current(scope=seeds.scope, context_key="t133")
            assert_conflict(caught.value, "CURRENT_CHECKPOINT_NOT_FOUND")
        finally:
            await engine.dispose()
    finally:
        await cleanup(session_factory, seeds)


# --- concurrency (Critical Gates 1 and 2, handoff 13 SS 135) -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_holding_evidence_first_is_seen_by_the_later_forget(
    session_factory: AsyncSessionFactory,
) -> None:
    """Gate 1, order 1: a Checkpoint Create share-locks Episode B and then waits
    on its stream lock; Forget(A) must wait for B, then see the committed
    statement and converge it -- the new dependency cannot escape."""

    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        body = checkpoint_body(
            seeds.scope,
            "g1a",
            current_state=statement("via B", support("all_required", ref("episode", b))),
        )
        results = await hold_and_run(
            "SELECT pg_advisory_xact_lock(:k)",
            {"k": context_stream_lock_key(seeds.scope, "g1a")},
            lambda: create_checkpoint(session_factory, body),
            lambda: eps.forget(a, idempotency_key=key()),
        )
        assert not any(isinstance(r, BaseException) for r in results), results
        created = results[0]
        final = await checkpoints(session_factory).get(created.checkpoint_id)
        assert final.lifecycle.value == "invalidated" and final.current_state is None
        assert_tombstone(await eps.get(b))
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_holding_evidence_first_rejects_the_later_create(
    session_factory: AsyncSessionFactory,
) -> None:
    """Gate 1, order 2: Forget(A) has scrubbed B and waits on a checkpoint lock
    (phase 2); an Episode Create referencing B waits on B, then sees the tombstone."""

    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        a = await new_episode(session_factory, seeds.scope)
        b = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", a))
        )
        cp0 = await create_checkpoint(
            session_factory,
            checkpoint_body(
                seeds.scope,
                "g1b",
                current_state=statement("estado"),
                pending=[statement("x", support("all_required", ref("episode", b)))],
            ),
        )
        results = await hold_and_run(
            "SELECT id FROM context_checkpoints WHERE id = :i FOR UPDATE",
            {"i": cp0.checkpoint_id},
            lambda: eps.forget(a, idempotency_key=key()),
            lambda: eps.create(
                episode_request(seeds.scope, support=support("all_required", ref("episode", b))),
                idempotency_key=key(),
            ),
        )
        assert not isinstance(results[0], BaseException), results[0]
        assert_conflict(results[1], "EVIDENCE_UNAVAILABLE")
        after = await checkpoints(session_factory).get(cp0.checkpoint_id)
        assert after.lifecycle.value == "active" and after.pending_work == []
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_rank_lock_order_prevents_create_forget_deadlock(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 2: B depends on A but sorts *before* A by UUID. A Create citing both
    is paused right after its first Episode share lock while Forget(A) runs.
    Locking by id (B, then A) would deadlock against Forget (A, then B); the
    (rank, id) order locks A first, so Forget simply waits."""

    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    rng = random.Random()
    a_id = UUID(int=rng.getrandbits(126) | (3 << 126))
    b_id = UUID(int=rng.getrandbits(120))
    try:
        a = await new_episode(session_factory, seeds.scope, episode_id=a_id)
        b = await new_episode(
            session_factory,
            seeds.scope,
            support("all_required", ref("episode", a)),
            episode_id=b_id,
        )
        assert b < a
        original = EvidenceResolver._resolve_type
        first_locked, release = asyncio.Event(), asyncio.Event()
        create_task: list[asyncio.Task[Any] | None] = [None]
        locked_order: list[UUID] = []

        async def paused(self: Any, source_type: Any, ids: Any, *, lock: bool) -> Any:
            out = await original(self, source_type, ids, lock=lock)
            if lock and source_type is T.EPISODE and asyncio.current_task() is create_task[0]:
                locked_order.extend(ids)
                if not first_locked.is_set():
                    first_locked.set()
                    await release.wait()
            return out

        monkeypatch.setattr(EvidenceResolver, "_resolve_type", paused)
        engine = create_async_engine(_test_database_url(dict(os.environ)))
        try:
            async with engine.connect() as probe:
                task = asyncio.ensure_future(
                    eps.create(
                        episode_request(
                            seeds.scope,
                            support=support("all_required", ref("episode", a), ref("episode", b)),
                        ),
                        idempotency_key=key(),
                    )
                )
                create_task[0] = task
                await asyncio.wait_for(first_locked.wait(), 30)
                forget = asyncio.ensure_future(eps.forget(a, idempotency_key=key()))
                for _ in range(500):
                    if await lock_waiters(probe) >= 1:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("Forget never waited on Episode A")
                release.set()
                created, forgotten = await asyncio.wait_for(
                    asyncio.gather(task, forget, return_exceptions=True), 60
                )
        finally:
            await engine.dispose()
        assert locked_order == [a, b]
        assert not isinstance(created, BaseException), created
        assert not isinstance(forgotten, BaseException), forgotten
        for scrubbed in (a, b, created.episode_id):
            assert_tombstone(await eps.get(scrubbed))
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_forgets_converging_on_one_owner_never_leave_it_unsupported(
    session_factory: AsyncSessionFactory,
) -> None:
    """SS 135 / write skew: B any_sufficient [M1, M2]; Forget(M1) and Forget(M2)
    both reach B. B's row lock serializes the two evaluations, so the second
    sees the first's committed loss and scrubs B."""

    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        m1 = await create_memory(session_factory, seeds.scope)
        m2 = await create_memory(session_factory, seeds.scope)
        b = await new_episode(
            session_factory,
            seeds.scope,
            support("any_sufficient", ref("memory_item", m1), ref("memory_item", m2)),
        )
        results = await run_with_row_held(
            "episodes",
            b,
            lambda: memories(session_factory).forget(m1, idempotency_key=None),
            lambda: memories(session_factory).forget(m2, idempotency_key=None),
        )
        assert not any(isinstance(r, BaseException) for r in results), results
        assert_tombstone(await eps.get(b))
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("victim", ["create", "forget"])
async def test_stale_rank_view_deadlock_is_transient_and_rolls_back_completely(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch, victim: str
) -> None:
    """Residual risk (pre-validation review, Task 2), reproduced deterministically.

    D and Q are *incomparable* (neither depends on the other), so their relative
    rank can change when a concurrent Forget commits the removal of an ancestor's
    outgoing links. A Create that computed ranks before that commit locks Q then
    D; a Forget(Y) that computed them after locks D then Q -> a genuine
    PostgreSQL deadlock. The victim's whole transaction rolls back: no partial
    scrub, no successful parent Forget, 503 DEPENDENCY_UNAVAILABLE, and a retry
    with the same key converges without resurrection."""

    from sofias_memory.infrastructure.postgres.repositories.contextual_evidence import (
        ContextualEvidenceRepository,
    )

    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    try:
        v0 = await new_episode(session_factory, seeds.scope)
        v1 = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", v0))
        )
        x = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", v1))
        )
        y = await new_episode(session_factory, seeds.scope)
        d = await new_episode(
            session_factory,
            seeds.scope,
            support("any_sufficient", ref("episode", x), ref("episode", y)),
        )  # rank 3 now, rank 1 once X's links are released
        z0 = await new_episode(session_factory, seeds.scope)
        z = await new_episode(
            session_factory, seeds.scope, support("all_required", ref("episode", z0))
        )
        q = await new_episode(
            session_factory,
            seeds.scope,
            support("all_required", ref("episode", y), ref("episode", z)),
        )  # rank 2, stable

        create_ranked, create_go = asyncio.Event(), asyncio.Event()
        forget_locked, forget_go = asyncio.Event(), asyncio.Event()
        tasks: dict[str, asyncio.Task[Any] | None] = {"create": None, "forget": None}
        orders: dict[str, list[UUID]] = {}

        original_order = EvidenceResolver.episode_lock_order

        async def paused_order(self: Any, episode_ids: Any) -> list[UUID]:
            order = await original_order(self, episode_ids)
            if asyncio.current_task() is tasks["create"]:
                orders["create"] = order
                create_ranked.set()
                await create_go.wait()
            return order

        original_lock = ContextualEvidenceRepository.lock_episode_for_propagation

        async def paused_lock(self: Any, episode_id: UUID) -> Any:
            out = await original_lock(self, episode_id)
            if asyncio.current_task() is tasks["forget"]:
                orders.setdefault("forget", []).append(episode_id)
                if not forget_locked.is_set():
                    forget_locked.set()
                    await forget_go.wait()
            return out

        monkeypatch.setattr(EvidenceResolver, "episode_lock_order", paused_order)
        monkeypatch.setattr(
            ContextualEvidenceRepository, "lock_episode_for_propagation", paused_lock
        )

        idem_y = key("deadlock-y")
        # PostgreSQL aborts the waiter whose deadlock_timeout fires first; giving
        # the Create's own connection a longer one makes the Forget the victim.
        from sofias_memory.infrastructure.postgres import create_session_factory

        create_engine_ = create_async_engine(
            _test_database_url(dict(os.environ)),
            connect_args={"server_settings": {"deadlock_timeout": "10s"}},
        )
        create_eps = episodes(create_session_factory(create_engine_)) if victim == "forget" else eps
        engine = create_async_engine(_test_database_url(dict(os.environ)))
        try:
            async with engine.connect() as probe:
                create = asyncio.ensure_future(
                    create_eps.create(
                        episode_request(
                            seeds.scope,
                            support=support("all_required", ref("episode", d), ref("episode", q)),
                        ),
                        idempotency_key=key("deadlock-c"),
                    )
                )
                tasks["create"] = create
                await asyncio.wait_for(create_ranked.wait(), 30)  # stale view: Q before D
                await eps.forget(x, idempotency_key=key())  # commits; D's rank drops to 1
                forget = asyncio.ensure_future(eps.forget(y, idempotency_key=idem_y))
                tasks["forget"] = forget
                await asyncio.wait_for(forget_locked.wait(), 30)  # fresh view: holds D
                create_go.set()  # Create: Q (share) ok, then D -> waits on Forget
                for _ in range(500):
                    if await lock_waiters(probe) >= 1:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("Create never waited on D")
                forget_go.set()  # Forget: Q (update) -> waits on Create => cycle
                created, forgotten = await asyncio.wait_for(
                    asyncio.gather(create, forget, return_exceptions=True), 60
                )
        finally:
            await engine.dispose()
            await create_engine_.dispose()
            monkeypatch.undo()

        assert orders["create"] == [q, d]
        assert orders["forget"][0] == d
        outcomes = [created, forgotten]
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(failures) == 1, outcomes  # exactly one deadlock victim
        assert_conflict(failures[0], "DEPENDENCY_UNAVAILABLE")
        cause = failures[0].__cause__
        assert isinstance(cause, DBAPIError) and transient_sqlstate(cause) == "40P01"
        assert isinstance(forgotten if victim == "forget" else created, BaseException)

        if isinstance(forgotten, BaseException):
            # Forget was the victim: nothing of it committed.
            assert_intact(await eps.get(y))
            assert_intact(await eps.get(d))
            assert_intact(await eps.get(q))
            assert (
                await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem_y))
                == 0
            )
            retried = await eps.forget(y, idempotency_key=idem_y)
            assert_tombstone(retried)
            for scrubbed in (d, q, created.episode_id):
                assert_tombstone(await eps.get(scrubbed))
        else:
            # Create was the victim: the Forget converged; a retried Create is refused.
            for scrubbed in (y, d, q):
                assert_tombstone(await eps.get(scrubbed))
            with pytest.raises(SofiasMemoryError) as caught:
                await eps.create(
                    episode_request(
                        seeds.scope,
                        support=support("all_required", ref("episode", d), ref("episode", q)),
                    ),
                    idempotency_key=key(),
                )
            assert_conflict(caught.value, "EVIDENCE_UNAVAILABLE")
        # Retrying never resurrects anything.
        assert_tombstone(await eps.get(x))
    finally:
        await cleanup(session_factory, seeds)
