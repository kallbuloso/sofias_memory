"""Real-PostgreSQL integration suite for SM-CTX-005 Knowledge Forget
propagation (handoff 13 SS 22-34/85-89/108-110/125-131, AC-005.1/15/18-21/28-30).

Runs the real Forget / Dataset Delete pipelines (real worker, all registered
steps) with Neo4j intentionally disabled (``graph_outbox_drain=None``, the
documented degrade): this proves the PostgreSQL authoritative boundary and
the contextual convergence, **not** Neo4j projection/reconciliation. Uses the
dedicated contextual test database without truncation; every dataset is
unique. Forget Everything necessarily reaches every dataset of that dedicated
database (synthetic test residue only).

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a dedicated
``DATABASE_URL`` (database name ending in ``_test``) migrated to head (0022).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.app import create_app
from sofias_memory.domain import PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres import create_session_factory
from sofias_memory.infrastructure.postgres.models import (
    ContextualMemoryIdempotency,
    GraphOutbox,
    PipelineRun,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.dataset_delete import (
    DATASET_DELETE_RESOURCES_RESOURCE,
    DatasetDeletePipelineResources,
)
from sofias_memory.pipelines.steps.forget import (
    FORGET_RESOURCES_RESOURCE,
    AuthoritativeMutationStep,
    ForgetPipelineResources,
)
from sofias_memory.services.contextual_evidence import EvidenceResolver
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder
from tests.integration.test_context_checkpoints_postgres_integration import (
    HMAC_KEY,
    _test_database_url,
    assert_conflict,
    key,
    new_scope,
)
from tests.integration.test_contextual_evidence_postgres_integration import (
    Seeds,
    checkpoints,
    cleanup,
    create_memory,
    episode_request,
    episodes,
    lock_waiters,
    ref,
    support,
)
from tests.integration.test_contextual_forget_propagation_postgres_integration import (
    assert_intact,
    assert_tombstone,
    checkpoint_body,
    create_checkpoint,
    hold_and_run,
    new_episode,
    statement,
)
from tests.integration.test_dataset_delete_postgres_integration import delete_dataset
from tests.integration.test_forget_postgres_integration import (
    POLL_INTERVAL_MS,
    get_source,
    post_forget,
    seed_dataset,
    seed_source_with_content,
    wait_for_terminal,
)
from tests.integration.test_forget_postgres_integration import make_settings as forget_settings

KNOWLEDGE_AGGREGATES = {"chunk", "entity", "entity_mention", "relation", "document", "summary"}


@pytest_asyncio.fixture()
async def knowledge_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_test_database_url(dict(os.environ)), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            name = str(await connection.scalar(text("SELECT current_database()")))
        if not name.endswith("_test"):
            pytest.skip("Knowledge propagation tests require a dedicated *_test database")
        yield engine
    finally:
        await engine.dispose()


def build_harness(
    engine: AsyncEngine, tmp_path: Path, *, worker_claims: bool = True
) -> tuple[Any, AsyncSessionFactory]:
    """Same composition as the Forget/Dataset Delete suites' harnesses (real
    registry and worker, ``enable_neo4j=False``, ``graph_outbox_processor=None``,
    no drain), with the HMAC key contextual services require."""

    settings = forget_settings(tmp_path, cognitive_idempotency_hmac_key=HMAC_KEY)
    factory = create_session_factory(engine)
    resources: dict[str, Any] = {
        DATASET_DELETE_RESOURCES_RESOURCE: DatasetDeletePipelineResources(
            settings=settings, graph_outbox_drain=None
        ),
        FORGET_RESOURCES_RESOURCE: ForgetPipelineResources(
            settings=settings, graph_outbox_drain=None
        ),
    }
    registry = build_default_pipeline_registry()
    coordinator = PipelineWorkerCoordinator(
        factory,
        registry if worker_claims else PipelineRegistry([]),
        enabled=True,
        poll_interval_ms=POLL_INTERVAL_MS,
        stale_after_seconds=settings.worker_stale_after_seconds,
        max_concurrent_datasets=settings.worker_max_concurrent_datasets,
        graph_outbox_processor=None,
        resources=resources,
    )
    app = create_app(
        settings,
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=factory,
        pipeline_registry=registry,
        pipeline_worker_coordinator=coordinator,
        process_state_holder=ProcessStateHolder(state=ProcessState.OPERATIONAL),
    )
    assert getattr(app.state, "neo4j_resource", None) is None
    return app, factory


# --- helpers -----------------------------------------------------------------------------


async def knowledge_ids(factory: AsyncSessionFactory, source_id: UUID) -> tuple[UUID, UUID]:
    async with factory() as s:
        document = await s.scalar(
            text("SELECT id FROM documents WHERE source_id = :s AND is_active ORDER BY id LIMIT 1"),
            {"s": source_id},
        )
        chunk = await s.scalar(
            text("SELECT id FROM chunks WHERE source_id = :s AND is_active ORDER BY id LIMIT 1"),
            {"s": source_id},
        )
    assert document is not None and chunk is not None
    return document, chunk


async def scalar_count(factory: AsyncSessionFactory, model: type, *where: Any) -> int:
    async with factory() as s:
        statement_ = select(func.count()).select_from(model)
        if where:
            statement_ = statement_.where(*where)
        return int(await s.scalar(statement_) or 0)


async def run_source_mutation(
    factory: AsyncSessionFactory,
    *,
    dataset_slug: str,
    dataset_id: UUID,
    source_id: UUID,
    memory_only: bool = False,
) -> dict[str, Any]:
    """Drives only the ``authoritative_mutation`` step (its own transaction), as
    the worker would -- the boundary where propagation must commit."""

    context = PipelineContext(
        run_id=uuid4(),
        pipeline_type=PipelineType.FORGET,
        dataset_id=dataset_id,
        source_id=source_id,
        run_input={
            "scope": "source",
            "dataset": dataset_slug,
            "source_id": str(source_id),
            "memory_only": memory_only,
        },
        step_outputs={},
        session_factory=factory,
        resources={},
    )
    result = type("R", (), {"output": {}})()
    async with PostgresUnitOfWork(factory) as uow:
        await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]
        await uow.commit()
    return dict(result.output)


async def contextual_cleanup(factory: AsyncSessionFactory, scope: str, ids: list[UUID]) -> None:
    """Removes this test's contextual rows and leaves its Knowledge Sources in a
    terminal tombstone shape (a non-terminal Source pointing at a pytest temp
    directory would otherwise block a later process's storage convergence)."""

    dummy = Seeds(
        scope=scope,
        session_id=uuid4(),
        session_entry=uuid4(),
        memory=uuid4(),
        dataset=uuid4(),
        source=uuid4(),
        document=uuid4(),
        chunk=uuid4(),
    )
    await cleanup(factory, dummy)
    async with factory() as s:
        await s.execute(
            text(
                "DELETE FROM contextual_evidence_refs r WHERE r.source_id = ANY(:ids) "
                "AND NOT EXISTS (SELECT 1 FROM episode_evidence_refs l "
                "WHERE l.evidence_ref_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM context_statement_evidence_refs l "
                "WHERE l.evidence_ref_id = r.id)"
            ),
            {"ids": ids},
        )
        await s.execute(
            text(
                "UPDATE sources SET status = 'deleted', storage_uri = NULL "
                "WHERE id = ANY(:ids) AND status <> 'deleted'"
            ),
            {"ids": ids},
        )
        await s.commit()


# --- Source Forget (full) -------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_full_forget_succeeded_implies_structural_refs_converged(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-src-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    old_document = uuid4()
    async with factory() as s:  # a second Document of the same Source, still active for now
        await s.execute(
            text(
                "INSERT INTO documents (id, dataset_id, source_id, generation, title, language, "
                "normalized_text, text_sha256, token_count, metadata, is_active) VALUES "
                "(:id, :d, :s, 0, 'old', 'en', 'old text', :sha, 1, '{}', true)"
            ),
            {"id": old_document, "d": dataset.id, "s": source.id, "sha": "d" * 64},
        )
        await s.commit()
    eps = episodes(factory)
    try:
        es = await new_episode(factory, scope, support("all_required", ref("source", source.id)))
        ed = await new_episode(factory, scope, support("all_required", ref("document", document)))
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        e_old = await new_episode(
            factory, scope, support("all_required", ref("document", old_document))
        )
        cp = await create_checkpoint(
            factory,
            checkpoint_body(
                scope,
                "k1",
                current_state=statement("estado", support("all_required", ref("chunk", chunk))),
            ),
        )
        async with factory() as s:  # the old Document stops being authoritative (not a Forget)
            await s.execute(
                text("UPDATE documents SET is_active = false WHERE id = :i"), {"i": old_document}
            )
            await s.commit()
        assert_intact(await eps.get(e_old))  # availability changed, no trigger yet
        runs_before = await scalar_count(factory, PipelineRun)
        async with factory() as s:
            outbox_max_before = int(
                await s.scalar(select(func.coalesce(func.max(GraphOutbox.id), 0))) or 0
            )
        ledger_before = await scalar_count(factory, ContextualMemoryIdempotency)

        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
            )
        finally:
            await coordinator.stop()
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "succeeded"

        for scrubbed in (es, ed, ec, e_old):  # historical/inactive Document included (SS 31)
            assert_tombstone(await eps.get(scrubbed))
        final = await checkpoints(factory).get(cp.checkpoint_id)
        assert final.lifecycle.value == "invalidated" and final.current_state is None
        assert (await get_source(factory, source.id)).status == SourceStatus.DELETED  # type: ignore[union-attr]
        # Only the parent run; graph events are Knowledge's own; no derived ledger rows.
        assert await scalar_count(factory, PipelineRun) == runs_before + 1
        async with factory() as s:
            new_events = (
                await s.execute(
                    select(GraphOutbox.aggregate_type, GraphOutbox.dataset_id).where(
                        GraphOutbox.id > outbox_max_before
                    )
                )
            ).all()
        assert new_events, "Knowledge Forget enqueues its own projection deletes"
        assert {row.dataset_id for row in new_events} == {dataset.id}
        assert {str(row.aggregate_type) for row in new_events} <= KNOWLEDGE_AGGREGATES
        assert await scalar_count(factory, ContextualMemoryIdempotency) == ledger_before
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk, old_document])


# --- memory_only + re-cognify non-resurrection ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_only_forget_scrubs_and_recognify_never_resurrects(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-mo-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    try:
        m = await create_memory(factory, scope)
        es = await new_episode(factory, scope, support("all_required", ref("source", source.id)))
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        e_any = await new_episode(
            factory, scope, support("any_sufficient", ref("chunk", chunk), ref("memory_item", m))
        )
        cp = await create_checkpoint(
            factory,
            checkpoint_body(
                scope,
                "k2",
                current_state=statement("estado", support("all_required", ref("memory_item", m))),
                pending=[statement("p", support("all_required", ref("chunk", chunk)))],
            ),
        )
        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app,
                {
                    "dataset": dataset.slug,
                    "source_id": str(source.id),
                    "memory_only": True,
                    "wait": True,
                },
            )
        finally:
            await coordinator.stop()
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "succeeded"
        assert (await get_source(factory, source.id)).status == SourceStatus.PENDING  # type: ignore[union-attr]

        assert_tombstone(await eps.get(es))
        assert_tombstone(await eps.get(ec))
        assert_intact(await eps.get(e_any))
        after = await checkpoints(factory).get(cp.checkpoint_id)
        assert after.lifecycle.value == "active" and after.pending_work == []

        # Re-cognify outcome (authoritative effect): Source ACTIVE again with new rows.
        new_document, new_chunk = uuid4(), uuid4()
        vector = "[" + ",".join(["0.0"] * 3072) + "]"
        async with factory() as s:
            await s.execute(
                text("UPDATE sources SET status = 'active' WHERE id = :i"), {"i": source.id}
            )
            await s.execute(
                text(
                    "INSERT INTO documents (id, dataset_id, source_id, generation, title, "
                    "language, normalized_text, text_sha256, token_count, metadata, is_active) "
                    "VALUES (:id, :d, :s, 0, 'new', 'en', 'new text', :sha, 1, '{}', true)"
                ),
                {"id": new_document, "d": dataset.id, "s": source.id, "sha": "e" * 64},
            )
            await s.execute(
                text(
                    "INSERT INTO chunks (id, dataset_id, document_id, source_id, generation, "
                    "ordinal, text, content_sha256, token_count, start_char, end_char, "
                    "section_path, metadata, embedding, lexical, is_active) VALUES "
                    "(:id, :d, :doc, :s, 0, 0, 'new', :sha, "
                    "1, 0, 3, '{}', '{}', CAST(:v AS vector), to_tsvector('simple', 'new'), true)"
                ),
                {
                    "id": new_chunk,
                    "d": dataset.id,
                    "doc": new_document,
                    "s": source.id,
                    "sha": "f" * 64,
                    "v": vector,
                },
            )
            await s.commit()
        await asyncio.sleep(0)
        assert_tombstone(await eps.get(es))  # never restored (SS 12/89)
        assert_tombstone(await eps.get(ec))
        assert (await checkpoints(factory).get(cp.checkpoint_id)).pending_work == []
        async with PostgresUnitOfWork(factory) as uow:
            from sofias_memory.domain import EvidenceKey, EvidenceSourceType

            resolved = await EvidenceResolver(uow).resolve(
                [
                    EvidenceKey(EvidenceSourceType.CHUNK, chunk),
                    EvidenceKey(EvidenceSourceType.SOURCE, source.id),
                ]
            )
        assert (
            resolved[EvidenceKey(EvidenceSourceType.CHUNK, chunk)].availability.value
            == "unavailable"
        )
        assert (
            resolved[EvidenceKey(EvidenceSourceType.SOURCE, source.id)].availability.value
            == "available"
        )
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


# --- failure after authoritative_mutation (AC-005.30) -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scrub_committed_by_authoritative_mutation_survives_a_later_failure(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SS 27-28: the step commits Knowledge deactivation *and* the contextual
    scrub; a crash before the remaining steps leaves the run unfinished but the
    scrub permanent. Resume converges to SUCCEEDED (idempotent)."""

    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-crash-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    try:
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        output = await run_source_mutation(
            factory, dataset_slug=dataset.slug, dataset_id=dataset.id, source_id=source.id
        )
        assert output["proceed"] is True
        # "Crash" here: no projection/storage/finalize step ran.
        assert (await get_source(factory, source.id)).status == SourceStatus.DELETING  # type: ignore[union-attr]
        assert_tombstone(await eps.get(ec))

        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
            )
        finally:
            await coordinator.stop()
        assert response.status_code == 200 and response.json()["data"]["status"] == "succeeded"
        assert_tombstone(await eps.get(ec))
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


# --- Dataset scope, Everything, Administrative Dataset Delete (AC-005.21) -----------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_converges_every_source_including_already_deleted(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-ds-{uuid4().hex[:12]}")
    s1, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    s2, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    s3, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    d1, c1 = await knowledge_ids(factory, s1.id)
    d2, c2 = await knowledge_ids(factory, s2.id)
    eps = episodes(factory)
    try:
        e1 = await new_episode(factory, scope, support("all_required", ref("chunk", c1)))
        e2 = await new_episode(factory, scope, support("all_required", ref("document", d2)))
        e3 = await new_episode(factory, scope, support("all_required", ref("source", s3.id)))
        async with factory() as s:  # s3 forgotten in the incremental era: e3 left behind
            await s.execute(
                text("UPDATE sources SET status = 'deleted', storage_uri = NULL WHERE id = :i"),
                {"i": s3.id},
            )
            await s.commit()
        assert_intact(await eps.get(e3))
        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(app, {"dataset": dataset.slug, "wait": True})
        finally:
            await coordinator.stop()
        assert response.status_code == 200, response.text
        for scrubbed in (e1, e2, e3):
            assert_tombstone(await eps.get(scrubbed))
    finally:
        await contextual_cleanup(factory, scope, [s1.id, s2.id, s3.id, d1, c1, d2, c2])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_everything_converges_across_datasets(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    da = await seed_dataset(factory, slug=f"ctx5-all-a-{uuid4().hex[:12]}")
    db = await seed_dataset(factory, slug=f"ctx5-all-b-{uuid4().hex[:12]}")
    sa, _ = await seed_source_with_content(factory, tmp_path, dataset_id=da.id)
    sb, _ = await seed_source_with_content(factory, tmp_path, dataset_id=db.id)
    _, ca = await knowledge_ids(factory, sa.id)
    dbd, _ = await knowledge_ids(factory, sb.id)
    eps = episodes(factory)
    try:
        ea = await new_episode(factory, scope, support("all_required", ref("chunk", ca)))
        eb = await new_episode(factory, scope, support("all_required", ref("document", dbd)))
        eab = await new_episode(
            factory, scope, support("any_sufficient", ref("chunk", ca), ref("document", dbd))
        )
        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"everything": True, "confirm": "DELETE EVERYTHING", "wait": True}
            )
        finally:
            await coordinator.stop()
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "succeeded"
        for scrubbed in (ea, eb, eab):
            assert_tombstone(await eps.get(scrubbed))
    finally:
        await contextual_cleanup(factory, scope, [sa.id, sb.id, ca, dbd])


async def _delete_and_wait(app: Any, factory: AsyncSessionFactory, dataset_id: UUID) -> Any:
    await app.state.pipeline_worker.start()
    try:
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["data"]["run_id"])
        return await wait_for_terminal(factory, run_id, timeout_seconds=60)
    finally:
        await app.state.pipeline_worker.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_administrative_dataset_delete_reuses_the_shared_propagation(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    """AC-005.21: DatasetDelete's ``deactivate_authoritative`` reuses
    ``apply_dataset_forget_mutation``, so propagation commits there. The Source
    carries no projectable content (no active chunk/entity), so with Neo4j
    disabled ``converge_projection`` is vacuous and the run reaches SUCCEEDED."""

    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-del-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    async with factory() as s:
        await s.execute(text("UPDATE chunks SET is_active = false WHERE id = :i"), {"i": chunk})
        await s.execute(
            text("UPDATE entities SET is_active = false WHERE dataset_id = :d"), {"d": dataset.id}
        )
        await s.commit()
    eps = episodes(factory)
    try:
        es = await new_episode(factory, scope, support("all_required", ref("source", source.id)))
        ed = await new_episode(factory, scope, support("all_required", ref("document", document)))
        status = await _delete_and_wait(app, factory, dataset.id)
        assert status is PipelineRunStatus.SUCCEEDED
        assert_tombstone(await eps.get(es))
        assert_tombstone(await eps.get(ed))
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_delete_failing_after_deactivation_keeps_the_committed_scrub(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    """AC-005.30 / SS 27-28 on DatasetDelete: with Neo4j disabled a populated
    dataset cannot prove projection convergence (ADR-0010 Finding 2), so the run
    FAILS at ``converge_projection`` -- after ``deactivate_authoritative`` already
    committed the contextual scrub, which stays permanent."""

    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-delf-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    try:
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        status = await _delete_and_wait(app, factory, dataset.id)
        assert status is PipelineRunStatus.FAILED
        assert_tombstone(await eps.get(ec))
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accepted_202_is_not_a_completion_guarantee(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    """AC-005.28: wait=false only means "accepted durably"; until the run's
    authoritative step commits, dependents are untouched."""

    app, factory = build_harness(knowledge_engine, tmp_path, worker_claims=False)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-202-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    try:
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        await app.state.pipeline_worker.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": False}
            )
            assert response.status_code == 202, response.text
            run_id = UUID(response.json()["data"]["run_id"])
            async with PostgresUnitOfWork(factory) as uow:
                run = await uow.pipeline_runs.get_by_id(run_id)
                assert run is not None and run.status is PipelineRunStatus.QUEUED
            assert_intact(await eps.get(ec))  # accepted, not propagated
            async with factory() as s:  # leave no claimable run behind in the shared test DB
                await s.execute(
                    text(
                        "UPDATE pipeline_runs SET status = 'cancelled', finished_at = now() "
                        "WHERE id = :i"
                    ),
                    {"i": run_id},
                )
                await s.commit()
        finally:
            await app.state.pipeline_worker.stop()
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


# --- Knowledge concurrency (Critical Gates 1 and 2) ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_first_then_source_forget_converges_the_new_statement(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-g1k-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    try:
        from sofias_memory.domain import context_stream_lock_key

        body = checkpoint_body(
            scope,
            "g1k",
            current_state=statement(
                "via Knowledge",
                support("all_required", ref("chunk", chunk), ref("source", source.id)),
            ),
        )
        results = await hold_and_run(
            "SELECT pg_advisory_xact_lock(:k)",
            {"k": context_stream_lock_key(scope, "g1k")},
            lambda: create_checkpoint(factory, body),
            lambda: run_source_mutation(
                factory, dataset_slug=dataset.slug, dataset_id=dataset.id, source_id=source.id
            ),
        )
        assert not any(isinstance(r, BaseException) for r in results), results
        final = await checkpoints(factory).get(results[0].checkpoint_id)
        assert final.lifecycle.value == "invalidated" and final.current_state is None
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_forget_first_rejects_the_later_create(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-g1k2-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    try:
        cp0 = await create_checkpoint(
            factory,
            checkpoint_body(
                scope,
                "g1k2",
                current_state=statement("estado"),
                pending=[statement("p", support("all_required", ref("chunk", chunk)))],
            ),
        )
        results = await hold_and_run(
            "SELECT id FROM context_checkpoints WHERE id = :i FOR UPDATE",
            {"i": cp0.checkpoint_id},
            lambda: run_source_mutation(
                factory, dataset_slug=dataset.slug, dataset_id=dataset.id, source_id=source.id
            ),
            lambda: eps.create(
                episode_request(
                    scope,
                    support=support("all_required", ref("chunk", chunk), ref("source", source.id)),
                ),
                idempotency_key=key(),
            ),
        )
        assert not isinstance(results[0], BaseException), results[0]
        assert_conflict(results[1], "EVIDENCE_UNAVAILABLE")
        assert (await checkpoints(factory).get(cp0.checkpoint_id)).pending_work == []
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_ancestor_first_share_order_prevents_deadlock(
    knowledge_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 2 (resolves the SM-CTX-004 DEFERRED ancestor question): a Create
    citing [chunk, source] is paused after its *first* Knowledge share lock
    while the Source Forget mutation runs. The old alphabetical order (chunk
    first) deadlocks against Forget (source, then chunk); the ancestor-first
    order (source first) makes Forget wait, then converge the new owner."""

    _, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-g2k-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    from sofias_memory.domain import EvidenceSourceType

    original = EvidenceResolver._resolve_type
    first_locked, release = asyncio.Event(), asyncio.Event()
    create_task: list[asyncio.Task[Any] | None] = [None]
    order: list[str] = []

    async def paused(self: Any, source_type: Any, ids: Any, *, lock: bool) -> Any:
        out = await original(self, source_type, ids, lock=lock)
        if (
            lock
            and asyncio.current_task() is create_task[0]
            and source_type
            in (
                EvidenceSourceType.SOURCE,
                EvidenceSourceType.CHUNK,
            )
        ):
            order.append(source_type.value)
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
                        scope,
                        support=support(
                            "all_required", ref("chunk", chunk), ref("source", source.id)
                        ),
                    ),
                    idempotency_key=key(),
                )
            )
            create_task[0] = task
            await asyncio.wait_for(first_locked.wait(), 30)
            mutation = asyncio.ensure_future(
                run_source_mutation(
                    factory, dataset_slug=dataset.slug, dataset_id=dataset.id, source_id=source.id
                )
            )
            for _ in range(500):
                if await lock_waiters(probe) >= 1:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Forget mutation never waited on the Source row")
            release.set()
            created, mutated = await asyncio.wait_for(
                asyncio.gather(task, mutation, return_exceptions=True), 60
            )
    finally:
        await engine.dispose()
        monkeypatch.undo()
    try:
        assert order == ["source", "chunk"]
        assert not isinstance(created, BaseException), created
        assert not isinstance(mutated, BaseException), mutated
        assert_tombstone(await eps.get(created.episode_id))
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


# --- transient retry through the real engine / atomic rollback before commit ------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transient_conflict_in_propagation_is_retried_by_the_engine_and_converges(
    knowledge_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine server-side 40P01 raised inside the ``authoritative_mutation``
    transaction during propagation is classified retryable; the real worker's
    bounded retry policy re-runs the step after the first attempt rolled back
    completely (Knowledge deactivation included), and the run converges."""

    from sofias_memory.services.contextual_evidence_invalidator import (
        ContextualEvidenceInvalidator,
    )

    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-retry-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    original = ContextualEvidenceInvalidator.propagate
    calls: list[dict[str, Any]] = []

    async def flaky(self: Any, seeds: Any, **kwargs: Any) -> Any:
        if not calls:
            calls.append({"attempt": 1})
            session = self._uow._require_session()
            await session.execute(
                text(
                    "DO $$ BEGIN RAISE EXCEPTION 'injected deadlock' "
                    "USING ERRCODE = '40P01'; END $$"
                )
            )
        async with factory() as s:  # what other transactions see right now
            seen = (
                await s.execute(
                    text(
                        "SELECT s.status::text, c.is_active FROM sources s "
                        "JOIN chunks c ON c.source_id = s.id WHERE s.id = :i AND c.id = :c"
                    ),
                    {"i": source.id, "c": chunk},
                )
            ).one()
        calls.append({"attempt": len(calls) + 1, "committed_view": [seen[0], seen[1]]})
        return await original(self, seeds, **kwargs)

    try:
        ec = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
        monkeypatch.setattr(ContextualEvidenceInvalidator, "propagate", flaky)
        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": False}
            )
            assert response.status_code == 202, response.text
            run_id = UUID(response.json()["data"]["run_id"])
            status = await wait_for_terminal(factory, run_id, timeout_seconds=90)
        finally:
            await coordinator.stop()
            monkeypatch.undo()
        assert status is PipelineRunStatus.SUCCEEDED
        async with PostgresUnitOfWork(factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None and run.attempt == 2  # one bounded retry
        # The retry saw the first attempt fully rolled back: Source still ACTIVE
        # and its chunk still active before the second mutation committed.
        assert calls[-1]["committed_view"] == ["active", True]
        assert_tombstone(await eps.get(ec))
        assert (await get_source(factory, source.id)).status == SourceStatus.DELETED  # type: ignore[union-attr]
    finally:
        await contextual_cleanup(factory, scope, [source.id, document, chunk])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corrupt_graph_fails_the_run_and_rolls_back_the_authoritative_mutation(
    knowledge_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Atomic rollback *before* commit: propagation corruption inside
    ``authoritative_mutation`` fails the run permanently with a sanitized code and
    nothing of that step commits -- no Knowledge deactivation, no scrub."""

    app, factory = build_harness(knowledge_engine, tmp_path)
    scope = new_scope()
    dataset = await seed_dataset(factory, slug=f"ctx5-corrupt-{uuid4().hex[:12]}")
    source, _ = await seed_source_with_content(factory, tmp_path, dataset_id=dataset.id)
    document, chunk = await knowledge_ids(factory, source.id)
    eps = episodes(factory)
    b = await new_episode(factory, scope, support("all_required", ref("chunk", chunk)))
    c = await new_episode(factory, scope, support("all_required", ref("episode", b)))
    try:
        async with factory() as s:  # B -> C closes a corrupt cycle
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
        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": False}
            )
            run_id = UUID(response.json()["data"]["run_id"])
            status = await wait_for_terminal(factory, run_id, timeout_seconds=30)
        finally:
            await coordinator.stop()
        assert status is PipelineRunStatus.FAILED
        async with PostgresUnitOfWork(factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None
            assert run.error_code == "FORGET_CONTEXTUAL_PROPAGATION_FAILED"
            assert "cycle" not in str(run.error_message or "").lower()
        assert (await get_source(factory, source.id)).status == SourceStatus.ACTIVE  # type: ignore[union-attr]
        async with factory() as s:
            active = await s.scalar(
                text("SELECT is_active FROM chunks WHERE id = :i"), {"i": chunk}
            )
        assert active is True
        assert_intact(await eps.get(b))
        assert_intact(await eps.get(c))
    finally:
        async with factory() as s:
            await s.execute(
                text(
                    "DELETE FROM episode_evidence_refs l USING contextual_evidence_refs r "
                    "WHERE l.episode_id = :b AND l.evidence_ref_id = r.id AND r.source_id = :c"
                ),
                {"b": b, "c": c},
            )
            await s.commit()
        await contextual_cleanup(factory, scope, [source.id, document, chunk])
