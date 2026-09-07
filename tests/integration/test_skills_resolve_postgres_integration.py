"""Real-PostgreSQL tests for the SM-704 semantic resolve service.

Proves ranking correctness/direction, the active-only filter (including
archive/restore), the current_revision pointer (never latest revision
blindly), top_k, deterministic tie-breaking, empty-candidate results, the
embedding-provider transaction boundary and failure atomicity, wrong-shape
rejection, absence of side effects on Query/SessionEntry/PipelineRun/
graph_outbox, and that the ANN query expression is compatible with the
HNSW ``halfvec_cosine_ops`` index the SM-701 migration created (ADR-0006)
-- all against a real PostgreSQL database and the real ``SkillService``,
with only the embedding provider faked. Requires migrations already
applied through 0014.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import pytest
import pytest_asyncio
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import cast as sql_cast
from sqlalchemy import delete, func, select, text

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.config import load_settings
from sofias_memory.domain import SkillStatus, build_skill_resolution_text
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    GraphOutbox,
    PipelineRun,
    Query,
    Skill,
    SkillRevision,
)
from sofias_memory.infrastructure.postgres.models import SessionEntry as SessionEntryModel
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillResolveRequest,
    SkillRevisionCreateRequest,
    SkillUpdateRequest,
)
from sofias_memory.services.skills import SkillService

POSTGRES_SKILLS_RESOLVE_ENV = "SOFIAS_MEMORY_RUN_SKILLS_RESOLVE_POSTGRES_TESTS"

EMBEDDING_DIMENSIONS = 3072


def _one_hot(index: int, *, magnitude: float = 1.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = magnitude
    return vector


VEC_A = _one_hot(0)
"""cosine(VEC_A, VEC_A) = 1.0 -- score 1.0."""

VEC_B = [0.0] * EMBEDDING_DIMENSIONS
VEC_B[0] = 0.5
VEC_B[1] = 0.5
"""cosine(VEC_A, VEC_B) = 0.5 / sqrt(0.5) ~= 0.7071 -- partial alignment."""

VEC_C = _one_hot(2)
"""cosine(VEC_A, VEC_C) = 0.0 -- orthogonal, score 0.0."""


class DeterministicEmbeddingClient:
    """Maps exact input text to a pre-registered vector -- lets a test
    control precisely what a Skill's resolution embedding (and the resolve
    query embedding) will be, without a real provider. Raises loudly on an
    unregistered text so a test never silently ranks against the wrong
    vector."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = dict(vectors)
        self.calls: list[str] = []

    def register(self, text: str, vector: list[float]) -> None:
        self._vectors[text] = vector

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for text_value in texts:
            self.calls.append(text_value)
            if text_value not in self._vectors:
                raise AssertionError(f"unregistered embedding text: {text_value!r}")
            result.append(self._vectors[text_value])
        return result


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


class ShapeEmbeddingClient:
    def __init__(self, response: list[list[float]]) -> None:
        self._response = response

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        return self._response


class BlockingEmbeddingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SKILLS_RESOLVE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_RESOLVE_ENV}=1 to run Skill resolve PostgreSQL tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup_skills(session_factory: AsyncSessionFactory, skill_ids: set[object]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def service(session_factory: AsyncSessionFactory, embedding_client: object) -> SkillService:
    return SkillService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


def resolution_text(name: str, description: str, tags: list[str] | None = None) -> str:
    return build_skill_resolution_text(name=name, description=description, tags=tags or [])


async def create_skill_with_vector(
    session_factory: AsyncSessionFactory,
    client: DeterministicEmbeddingClient,
    *,
    name: str,
    description: str,
    vector: list[float],
) -> object:
    client.register(resolution_text(name, description), vector)
    request = SkillCreateRequest(name=name, description=description, procedure="Do the thing.")  # type: ignore[call-arg]
    result = await service(session_factory, client).create_skill(request)
    return result.skill_uuid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_ranks_by_score_descending_correct_direction(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name_a = unique_name("resolve-a")
        name_b = unique_name("resolve-b")
        name_c = unique_name("resolve-c")
        skill_ids.add(
            await create_skill_with_vector(
                postgres_session_factory, client, name=name_a, description="A", vector=VEC_A
            )
        )
        skill_ids.add(
            await create_skill_with_vector(
                postgres_session_factory, client, name=name_b, description="B", vector=VEC_B
            )
        )
        skill_ids.add(
            await create_skill_with_vector(
                postgres_session_factory, client, name=name_c, description="C", vector=VEC_C
            )
        )

        client.register("find A", VEC_A)
        result = await service(postgres_session_factory, client).resolve(
            SkillResolveRequest(query="find A", top_k=20)
        )

        ours = [match for match in result.matches if match.skill_uuid in skill_ids]
        names_in_order = [match.name for match in ours]
        assert names_in_order == [name_a, name_b, name_c]
        scores = [match.score for match in ours]
        assert scores[0] > scores[1] > scores[2]
        assert scores[0] == pytest.approx(1.0, abs=1e-2)
        assert scores[1] == pytest.approx(0.7071, abs=1e-2)
        assert scores[2] == pytest.approx(0.0, abs=1e-2)
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_tie_break_is_deterministic_by_skill_id(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name_1 = unique_name("resolve-tie-1")
        name_2 = unique_name("resolve-tie-2")
        id_1 = await create_skill_with_vector(
            postgres_session_factory, client, name=name_1, description="tie", vector=VEC_A
        )
        id_2 = await create_skill_with_vector(
            postgres_session_factory, client, name=name_2, description="tie", vector=VEC_A
        )
        skill_ids.update({id_1, id_2})
        expected_order = sorted([id_1, id_2], key=str)

        client.register("tie query", VEC_A)
        result_1 = await service(postgres_session_factory, client).resolve(
            SkillResolveRequest(query="tie query", top_k=20)
        )
        result_2 = await service(postgres_session_factory, client).resolve(
            SkillResolveRequest(query="tie query", top_k=20)
        )

        observed_1 = [
            match.skill_uuid for match in result_1.matches if match.skill_uuid in {id_1, id_2}
        ]
        observed_2 = [
            match.skill_uuid for match in result_2.matches if match.skill_uuid in {id_1, id_2}
        ]
        assert observed_1 == expected_order
        assert observed_2 == expected_order
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_excludes_archived_and_restore_makes_eligible_again(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name_a = unique_name("resolve-archive-a")
        name_b = unique_name("resolve-archive-b")
        skill_a = await create_skill_with_vector(
            postgres_session_factory, client, name=name_a, description="A", vector=VEC_A
        )
        skill_b = await create_skill_with_vector(
            postgres_session_factory, client, name=name_b, description="B", vector=VEC_B
        )
        skill_ids.update({skill_a, skill_b})
        client.register("find A", VEC_A)

        def ours(matches: list[object]) -> list[str]:
            return [m.name for m in matches if m.skill_uuid in skill_ids]  # type: ignore[attr-defined]

        resolver = service(postgres_session_factory, client)
        before = await resolver.resolve(SkillResolveRequest(query="find A", top_k=20))
        assert ours(before.matches) == [name_a, name_b]

        await resolver.archive_skill(skill_a)
        after_archive = await resolver.resolve(SkillResolveRequest(query="find A", top_k=20))
        assert ours(after_archive.matches) == [name_b]

        await resolver.restore_skill(skill_a)
        after_restore = await resolver.resolve(SkillResolveRequest(query="find A", top_k=20))
        assert ours(after_restore.matches) == [name_a, name_b]
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_uses_current_revision_pointer_not_latest(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """revision 1 = deployment (~VEC_A), revision 2 = cooking (~VEC_C),
    current = revision 1 after an explicit rollback -- resolve must use
    revision 1's embedding/metadata, even though revision 2 is numerically
    newer."""

    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name = unique_name("resolve-pointer")
        skill_id = await create_skill_with_vector(
            postgres_session_factory, client, name=name, description="deployment", vector=VEC_A
        )
        skill_ids.add(skill_id)

        client.register(resolution_text(name, "cooking"), VEC_C)
        resolver = service(postgres_session_factory, client)
        await resolver.create_revision(
            skill_id,
            SkillRevisionCreateRequest(description="cooking", procedure="Do the thing."),  # type: ignore[call-arg]
        )
        # current_revision_id now points at revision 2 (cooking / VEC_C).

        await resolver.update_skill(skill_id, SkillUpdateRequest(current_revision=1))
        # rollback: current_revision_id now points back at revision 1 (deployment / VEC_A).

        client.register("find deployment", VEC_A)
        result = await resolver.resolve(SkillResolveRequest(query="find deployment", top_k=20))

        match = next(m for m in result.matches if m.skill_uuid == skill_id)
        assert match.description == "deployment"
        assert match.current_revision == 1
        assert match.score == pytest.approx(1.0, abs=1e-2)
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_no_candidates_returns_empty_matches(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({"nobody home": VEC_A})
    result = await service(postgres_session_factory, client).resolve(
        SkillResolveRequest(query="nobody home", top_k=5)
    )
    assert result.matches == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_all_archived_returns_empty_matches(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name = unique_name("resolve-all-archived")
        skill_id = await create_skill_with_vector(
            postgres_session_factory, client, name=name, description="A", vector=VEC_A
        )
        skill_ids.add(skill_id)
        resolver = service(postgres_session_factory, client)
        await resolver.archive_skill(skill_id)

        client.register("find A", VEC_A)
        result = await resolver.resolve(SkillResolveRequest(query="find A", top_k=20))
        assert result.matches == []
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_respects_top_k_limit(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        for index in range(3):
            name = unique_name(f"resolve-topk-{index}")
            vector = _one_hot(index, magnitude=1.0)
            skill_ids.add(
                await create_skill_with_vector(
                    postgres_session_factory, client, name=name, description="x", vector=vector
                )
            )

        client.register("q", VEC_A)
        result = await service(postgres_session_factory, client).resolve(
            SkillResolveRequest(query="q", top_k=2)
        )
        assert len(result.matches) == 2
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_no_side_effects_on_other_tables(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    async def counts() -> tuple[int, int, int, int]:
        async with postgres_session_factory() as session:
            queries = await session.scalar(select(func.count()).select_from(Query))
            entries = await session.scalar(select(func.count()).select_from(SessionEntryModel))
            runs = await session.scalar(select(func.count()).select_from(PipelineRun))
            outbox = await session.scalar(select(func.count()).select_from(GraphOutbox))
            return int(queries or 0), int(entries or 0), int(runs or 0), int(outbox or 0)

    try:
        name = unique_name("resolve-side-effects")
        skill_id = await create_skill_with_vector(
            postgres_session_factory, client, name=name, description="A", vector=VEC_A
        )
        skill_ids.add(skill_id)
        client.register("find A", VEC_A)

        before = await counts()
        await service(postgres_session_factory, client).resolve(
            SkillResolveRequest(query="find A", top_k=5)
        )
        after = await counts()
        assert before == after
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_embeds_raw_query_text_never_resolution_text(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({"how do I deploy a Laravel app?": VEC_A})
    await service(postgres_session_factory, client).resolve(
        SkillResolveRequest(query="how do I deploy a Laravel app?", top_k=5)
    )
    assert client.calls == ["how do I deploy a Laravel app?"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_provider_call_holds_no_transaction_or_row_lock(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name = unique_name("resolve-boundary")
        skill_id = await create_skill_with_vector(
            postgres_session_factory, client, name=name, description="A", vector=VEC_A
        )
        skill_ids.add(skill_id)

        blocking = BlockingEmbeddingClient()
        resolver = service(postgres_session_factory, blocking)
        task = asyncio.create_task(resolver.resolve(SkillResolveRequest(query="anything", top_k=5)))
        try:
            await asyncio.wait_for(blocking.started.wait(), timeout=5)

            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                locked = await asyncio.wait_for(
                    uow.skills.get_by_id_for_update(skill_id), timeout=2
                )
                assert locked is not None
                await uow.commit()
        finally:
            blocking.release.set()

        await asyncio.wait_for(task, timeout=5)
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_provider_failure_returns_dependency_unavailable(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    resolver = service(postgres_session_factory, FailingEmbeddingClient())
    with pytest.raises(DependencyUnavailableError):
        await resolver.resolve(SkillResolveRequest(query="anything", top_k=5))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_wrong_embedding_shape_rejected(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dimensions = EMBEDDING_DIMENSIONS
    malformed_responses = {
        "zero": [],
        "multi": [[0.1] * dimensions, [0.1] * dimensions],
        "low": [[0.1] * (dimensions - 1)],
        "high": [[0.1] * (dimensions + 1)],
    }
    for label, response in malformed_responses.items():
        resolver = service(postgres_session_factory, ShapeEmbeddingClient(response))
        try:
            await resolver.resolve(SkillResolveRequest(query="anything", top_k=5))
        except DependencyUnavailableError:
            pass
        else:
            pytest.fail(f"expected DependencyUnavailableError for shape {label!r}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_ann_query_expression_compatible_with_hnsw_halfvec_index(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Structural proof (ADR-0006): forcing the planner away from a
    sequential scan (``enable_seqscan = off``) must still yield a valid
    plan that uses the ``ix_skill_revisions_resolution_embedding_halfvec_hnsw``
    index -- this proves the generated SQL expression
    (``resolution_embedding::halfvec(3072) <=> query::halfvec(3072)``) is
    compatible with the ``halfvec_cosine_ops`` HNSW expression index, not
    that a natural (un-forced) plan on a tiny dataset would choose it."""

    client = DeterministicEmbeddingClient({})
    skill_ids: set[object] = set()

    try:
        name = unique_name("resolve-explain")
        skill_id = await create_skill_with_vector(
            postgres_session_factory, client, name=name, description="A", vector=VEC_A
        )
        skill_ids.add(skill_id)

        distance = sql_cast(
            SkillRevision.resolution_embedding, HALFVEC(EMBEDDING_DIMENSIONS)
        ).cosine_distance(VEC_A)
        statement = (
            select(Skill.id, distance.label("distance"))
            .select_from(Skill)
            .join(SkillRevision, SkillRevision.id == Skill.current_revision_id)
            .where(Skill.status == SkillStatus.ACTIVE)
            .order_by(distance.asc(), Skill.id.asc())
            .limit(5)
        )
        compiled = statement.compile(compile_kwargs={"literal_binds": True})

        async with postgres_session_factory() as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            result = await session.execute(text(f"EXPLAIN {compiled}"))
            plan_lines = [row[0] for row in result]
        plan_text = "\n".join(plan_lines)
        assert "ix_skill_revisions_resolution_embedding_halfvec_hnsw" in plan_text
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)
