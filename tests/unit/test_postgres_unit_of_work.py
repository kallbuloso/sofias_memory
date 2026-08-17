from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    Feedback,
    GraphOutbox,
    PipelineRun,
    PipelineStep,
    Relation,
    RelationEvidence,
    Source,
)
from sofias_memory.infrastructure.postgres.repositories import (
    ChunkRepository,
    DatasetRepository,
    DocumentRepository,
    EntityMentionRepository,
    EntityRepository,
    FeedbackRepository,
    GraphOutboxRepository,
    PipelineRunRepository,
    PipelineStepRepository,
    RelationEvidenceRepository,
    RelationRepository,
    SourceRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork


class FakeAsyncSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.scalar_result: object | None = None
        self.scalars_result: tuple[object, ...] = ()
        self.scalar_calls = 0
        self.scalars_calls = 0

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def scalar(self, statement: object) -> object | None:
        self.scalar_calls += 1
        return self.scalar_result

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.scalars_calls += 1
        return self.scalars_result


def make_session_factory(fake_session: FakeAsyncSession) -> AsyncSessionFactory:
    def session_factory() -> AsyncSession:
        return cast(AsyncSession, fake_session)

    return cast(AsyncSessionFactory, session_factory)


def repository_sessions(uow: PostgresUnitOfWork) -> list[object]:
    return [
        uow.datasets._session,
        uow.sources._session,
        uow.documents._session,
        uow.chunks._session,
        uow.entities._session,
        uow.entity_mentions._session,
        uow.relations._session,
        uow.relation_evidence._session,
        uow.pipeline_runs._session,
        uow.pipeline_steps._session,
        uow.queries._session,
        uow.feedback._session,
        uow.graph_outbox._session,
    ]


@pytest.mark.asyncio
async def test_unit_of_work_wires_repositories_to_one_shared_session() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    async with uow:
        assert repository_sessions(uow) == [fake_session] * 13


@pytest.mark.asyncio
async def test_unit_of_work_requires_explicit_commit_to_persist() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    async with uow:
        pass

    assert fake_session.commit_calls == 0
    assert fake_session.rollback_calls == 1
    assert fake_session.close_calls == 1


@pytest.mark.asyncio
async def test_unit_of_work_explicit_commit_does_not_rollback_on_success() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    async with uow:
        await uow.commit()

    assert fake_session.commit_calls == 1
    assert fake_session.rollback_calls == 0
    assert fake_session.close_calls == 1


@pytest.mark.asyncio
async def test_unit_of_work_flushes_without_committing() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    async with uow:
        await uow.flush()
        assert fake_session.flush_calls == 1
        assert fake_session.commit_calls == 0

    assert fake_session.rollback_calls == 1
    assert fake_session.close_calls == 1


@pytest.mark.asyncio
async def test_unit_of_work_explicit_rollback_closes_without_second_rollback() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    async with uow:
        await uow.rollback()

    assert fake_session.commit_calls == 0
    assert fake_session.rollback_calls == 1
    assert fake_session.close_calls == 1


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_and_propagates_exception() -> None:
    fake_session = FakeAsyncSession()
    uow = PostgresUnitOfWork(make_session_factory(fake_session))

    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            raise RuntimeError("boom")

    assert fake_session.commit_calls == 0
    assert fake_session.rollback_calls == 1
    assert fake_session.close_calls == 1


@pytest.mark.asyncio
async def test_unit_of_work_repositories_are_unavailable_outside_context() -> None:
    uow = PostgresUnitOfWork(make_session_factory(FakeAsyncSession()))

    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.datasets

    async with uow:
        _ = uow.datasets

    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.datasets


@pytest.mark.asyncio
async def test_repository_add_flushes_but_never_commits_or_rolls_back() -> None:
    fake_session = FakeAsyncSession()
    session = cast(AsyncSession, fake_session)
    repositories_and_models = [
        (DatasetRepository(session), cast(Dataset, object())),
        (SourceRepository(session), cast(Source, object())),
        (DocumentRepository(session), cast(Document, object())),
        (ChunkRepository(session), chunk_model()),
        (EntityRepository(session), cast(Entity, object())),
        (EntityMentionRepository(session), cast(EntityMention, object())),
        (RelationRepository(session), cast(Relation, object())),
        (RelationEvidenceRepository(session), cast(RelationEvidence, object())),
        (PipelineRunRepository(session), cast(PipelineRun, object())),
        (PipelineStepRepository(session), cast(PipelineStep, object())),
        (FeedbackRepository(session), cast(Feedback, object())),
        (GraphOutboxRepository(session), cast(GraphOutbox, object())),
    ]

    for repository, model in repositories_and_models:
        await repository.add(model)

    assert fake_session.added == [model for _, model in repositories_and_models]
    assert fake_session.flush_calls == len(repositories_and_models)
    assert fake_session.commit_calls == 0
    assert fake_session.rollback_calls == 0


def chunk_model() -> Chunk:
    return Chunk(
        id=uuid4(),
        dataset_id=uuid4(),
        document_id=uuid4(),
        source_id=uuid4(),
        generation=0,
        ordinal=0,
        text="chunk text",
        content_sha256="a" * 64,
        token_count=2,
        start_char=0,
        end_char=10,
        section_path=[],
        metadata_={},
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )


@pytest.mark.asyncio
async def test_repository_lookup_methods_return_scalar_results() -> None:
    fake_session = FakeAsyncSession()
    session = cast(AsyncSession, fake_session)
    expected = object()
    fake_session.scalar_result = expected

    assert await DatasetRepository(session).get_by_id(uuid4()) is expected
    assert await DatasetRepository(session).get_by_slug("dataset") is expected
    assert await SourceRepository(session).get_by_id(uuid4()) is expected
    assert (
        await SourceRepository(session).get_by_content_hash(
            dataset_id=uuid4(),
            content_sha256="a" * 64,
            version=1,
        )
        is expected
    )
    assert await DocumentRepository(session).get_by_id(uuid4()) is expected
    assert await ChunkRepository(session).exists_for_source_generation(
        source_id=uuid4(),
        generation=0,
    )
    assert await ChunkRepository(session).get_by_id(uuid4()) is expected
    assert (
        await EntityRepository(session).get_active_by_canonical_key(
            dataset_id=uuid4(), canonical_key="technology:postgresql"
        )
        is expected
    )
    assert await EntityMentionRepository(session).exists_for_entity_chunk(
        entity_id=uuid4(), chunk_id=uuid4()
    )
    assert (
        await RelationRepository(session).get_active_by_identity(
            source_entity_id=uuid4(),
            target_entity_id=uuid4(),
            predicate="uses",
            generation=0,
        )
        is expected
    )
    assert await RelationEvidenceRepository(session).exists_for_relation_chunk(
        relation_id=uuid4(), chunk_id=uuid4()
    )
    assert await PipelineRunRepository(session).get_by_id(uuid4()) is expected
    assert await PipelineRunRepository(session).get_by_idempotency_key("key") is expected
    assert await PipelineStepRepository(session).get_by_id(uuid4()) is expected
    assert await FeedbackRepository(session).get_by_id(uuid4()) is expected
    assert await GraphOutboxRepository(session).get_by_id(1) is expected
    assert fake_session.scalar_calls == 16


@pytest.mark.asyncio
async def test_repository_list_methods_return_deterministic_scalar_lists() -> None:
    fake_session = FakeAsyncSession()
    session = cast(AsyncSession, fake_session)
    first = object()
    second = object()
    fake_session.scalars_result = (first, second)

    assert await DocumentRepository(session).list_for_source(uuid4()) == [first, second]
    assert await ChunkRepository(session).list_for_source_generation(
        source_id=uuid4(),
        generation=0,
    ) == [first, second]
    assert await PipelineStepRepository(session).list_for_run(uuid4()) == [first, second]
    assert fake_session.scalars_calls == 3
