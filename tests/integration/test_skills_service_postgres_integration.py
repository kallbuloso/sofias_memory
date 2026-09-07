"""Real-PostgreSQL tests for the SM-702 Skill management service.

Proves the embedding write path (Feature Contract SS 7.1), safe replay
(including historical replay), archive-as-discovery-filter semantics
(including creating a revision while archived), current_revision rollback
(including its concurrency with new-revision creation, both linearization
orders), timestamp no-op discipline, provider-failure atomicity, resolution
text content, and duplicate-name creation concurrency -- all against a real
PostgreSQL database and the real ``SkillService``, with only the embedding
provider faked. Requires migrations already applied through 0014.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import load_settings
from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillRevisionCreateRequest,
    SkillUpdateRequest,
)
from sofias_memory.services.skills import SkillService

POSTGRES_SKILLS_API_ENV = "SOFIAS_MEMORY_RUN_SKILLS_MANAGEMENT_POSTGRES_TESTS"

EMBEDDING_DIMENSIONS = 3072


class FakeEmbeddingClient:
    def __init__(self, *, dimensions: int = EMBEDDING_DIMENSIONS, fail: bool = False) -> None:
        self._dimensions = dimensions
        self._fail = fail
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            raise ConnectionError("simulated embedding provider failure")
        return [[0.125] * self._dimensions for _ in texts]


class ShapeEmbeddingClient:
    """Returns a fixed, caller-supplied embedding response regardless of the
    input texts -- used to prove malformed provider responses (wrong vector
    count, wrong dimension) are rejected before any PostgreSQL write."""

    def __init__(self, response: list[list[float]]) -> None:
        self._response = response

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        return self._response


class BlockingEmbeddingClient:
    """Blocks inside ``embed_texts`` until the test releases it -- used to
    prove no PostgreSQL transaction/row lock is held across the call."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SKILLS_API_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_API_ENV}=1 to run Skill management PostgreSQL tests")

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


def build_create_request(**overrides: object) -> SkillCreateRequest:
    base: dict[str, object] = {
        "name": unique_name("sm702"),
        "description": "A description.",
        "procedure": "Do the thing.",
    }
    base.update(overrides)
    return SkillCreateRequest(**base)  # type: ignore[arg-type]


def build_revision_request(**overrides: object) -> SkillRevisionCreateRequest:
    base: dict[str, object] = {
        "description": "A description.",
        "procedure": "Do the thing.",
    }
    base.update(overrides)
    return SkillRevisionCreateRequest(**base)  # type: ignore[arg-type]


def service(session_factory: AsyncSessionFactory, embedding_client: object) -> SkillService:
    return SkillService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


# --- embedding write path / resolution text -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_skill_embeds_name_description_tags_only(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    fake = FakeEmbeddingClient()
    name = unique_name("sm702-resolution")
    request = build_create_request(
        name=name,
        description="SENTINEL_DESCRIPTION",
        procedure="SENTINEL_PROCEDURE_MUST_NOT_LEAK",
        license="SENTINEL_LICENSE_MUST_NOT_LEAK",
        compatibility="SENTINEL_COMPAT_MUST_NOT_LEAK",
        metadata={"k": "SENTINEL_METADATA_MUST_NOT_LEAK"},
        tags=["zeta", "alpha"],
        declared_tools=["SENTINEL_TOOL_MUST_NOT_LEAK"],
    )

    result = await service(postgres_session_factory, fake).create_skill(request)
    try:
        assert len(fake.calls) == 1
        (text,) = fake.calls[0]
        assert text == f"{name}\nSENTINEL_DESCRIPTION\nalpha, zeta"
        assert "SENTINEL_PROCEDURE_MUST_NOT_LEAK" not in text
        assert "SENTINEL_LICENSE_MUST_NOT_LEAK" not in text
        assert "SENTINEL_COMPAT_MUST_NOT_LEAK" not in text
        assert "SENTINEL_METADATA_MUST_NOT_LEAK" not in text
        assert "SENTINEL_TOOL_MUST_NOT_LEAK" not in text
    finally:
        await cleanup_skills(postgres_session_factory, {result.skill_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_call_holds_no_transaction_or_row_lock(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    # Create a Skill first (with a fast fake), then start a revision create
    # whose embedding call blocks -- while it's blocked, prove a second,
    # independent connection can immediately FOR UPDATE NOWAIT the same
    # Skill row: if any lock/transaction were mistakenly held across the
    # provider call, this would fail instead of succeeding immediately.
    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request()
    )
    skill_id = created.skill_uuid

    try:
        blocking = BlockingEmbeddingClient()
        skill_service = service(postgres_session_factory, blocking)

        task = asyncio.create_task(
            skill_service.create_revision(skill_id, build_revision_request(description="v2"))
        )
        try:
            await asyncio.wait_for(blocking.started.wait(), timeout=5)

            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                # A real row lock probe: FOR UPDATE (no NOWAIT support is
                # exposed by the repository, so a plain get_by_id_for_update
                # completing promptly is itself the proof -- if the first
                # task held the lock, this would hang until the blocking
                # embedding client is released, which we haven't done yet.
                locked = await asyncio.wait_for(
                    uow.skills.get_by_id_for_update(skill_id), timeout=2
                )
                assert locked is not None
                await uow.commit()
        finally:
            blocking.release.set()

        result, created_flag = await asyncio.wait_for(task, timeout=5)
        assert created_flag is True
        assert result.description == "v2"
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failure_on_create_leaves_no_rows(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    fake = FakeEmbeddingClient(fail=True)
    name = unique_name("sm702-fail-create")

    with pytest.raises(DependencyUnavailableError):
        await service(postgres_session_factory, fake).create_skill(build_create_request(name=name))

    async with postgres_session_factory() as session:
        found_skill = await session.scalar(select(Skill).where(Skill.name == name))
        assert found_skill is None
        # No Skill row exists to own one -- a SkillRevision FK-referencing
        # this name can therefore not exist either; asserted explicitly to
        # document the invariant rather than leave it merely implied.
        revision_join = select(SkillRevision).join(Skill, SkillRevision.skill_id == Skill.id)
        found_revisions = await session.scalars(revision_join.where(Skill.name == name))
        assert found_revisions.all() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failure_on_create_revision_leaves_skill_unchanged(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request()
    )
    skill_id = created.skill_uuid

    try:
        fake = FakeEmbeddingClient(fail=True)
        with pytest.raises(DependencyUnavailableError):
            await service(postgres_session_factory, fake).create_revision(
                skill_id, build_revision_request(description="v2 will fail")
            )

        fetched = await service(postgres_session_factory, FakeEmbeddingClient()).get_skill(skill_id)
        assert fetched.current_revision == 1
        assert fetched.description == "A description."
        assert fetched.updated_at == created.updated_at

        revisions = await service(postgres_session_factory, FakeEmbeddingClient()).list_revisions(
            skill_id, limit=50, offset=0
        )
        assert revisions.total == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_shape_failure_on_create_leaves_no_rows(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dimensions = load_settings().embedding_dimensions
    malformed_responses = {
        "zero": [],
        "multi": [[0.1] * dimensions, [0.1] * dimensions],
        "low": [[0.1] * (dimensions - 1)],
        "high": [[0.1] * (dimensions + 1)],
    }

    for label, response in malformed_responses.items():
        name = unique_name(f"shape-{label}")
        fake = ShapeEmbeddingClient(response)

        with pytest.raises(DependencyUnavailableError):
            await service(postgres_session_factory, fake).create_skill(
                build_create_request(name=name)
            )

        async with postgres_session_factory() as session:
            found = await session.scalar(select(Skill).where(Skill.name == name))
            assert found is None, label


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_shape_failure_on_create_revision_leaves_skill_unchanged(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dimensions = load_settings().embedding_dimensions
    malformed_responses = {
        "zero_vectors": [],
        "multiple_vectors": [[0.1] * dimensions, [0.1] * dimensions],
        "dimension_too_low": [[0.1] * (dimensions - 1)],
        "dimension_too_high": [[0.1] * (dimensions + 1)],
    }

    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request()
    )
    skill_id = created.skill_uuid

    try:
        for label, response in malformed_responses.items():
            fake = ShapeEmbeddingClient(response)
            with pytest.raises(DependencyUnavailableError):
                await service(postgres_session_factory, fake).create_revision(
                    skill_id, build_revision_request(description=f"v-{label} will fail")
                )

            fetched = await service(postgres_session_factory, FakeEmbeddingClient()).get_skill(
                skill_id
            )
            assert fetched.current_revision == 1, label
            assert fetched.updated_at == created.updated_at, label

            revisions = await service(
                postgres_session_factory, FakeEmbeddingClient()
            ).list_revisions(skill_id, limit=50, offset=0)
            assert revisions.total == 1, label
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


# --- safe replay / historical replay ----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_revision_semantic_replay_returns_existing_without_moving_current(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request(description="rev1")
    )
    skill_id = created.skill_uuid

    try:
        result_a, created_a = await service(
            postgres_session_factory, FakeEmbeddingClient()
        ).create_revision(skill_id, build_revision_request(description="rev1"))
        assert created_a is False  # identical to revision 1
        assert result_a.revision == 1

        skill = await service(postgres_session_factory, FakeEmbeddingClient()).get_skill(skill_id)
        assert skill.current_revision == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_historical_content_replay_never_moves_current(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request(description="content A")
    )
    skill_id = created.skill_uuid

    try:
        result_b, created_b = await service(
            postgres_session_factory, FakeEmbeddingClient()
        ).create_revision(skill_id, build_revision_request(description="content B"))
        assert created_b is True
        assert result_b.revision == 2

        # Resubmit revision 1's exact content while current is revision 2.
        replay, created_replay = await service(
            postgres_session_factory, FakeEmbeddingClient()
        ).create_revision(skill_id, build_revision_request(description="content A"))
        assert created_replay is False
        assert replay.revision == 1

        skill = await service(postgres_session_factory, FakeEmbeddingClient()).get_skill(skill_id)
        assert skill.current_revision == 2  # unchanged by the replay
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


# --- archive / restore / create-while-archived ------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_management_matrix(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created = await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
        build_create_request(description="v1")
    )
    skill_id = created.skill_uuid

    try:
        skill_service = service(postgres_session_factory, FakeEmbeddingClient())

        archived = await skill_service.archive_skill(skill_id)
        assert archived.status == SkillStatus.ARCHIVED
        assert archived.archived_at is not None

        # GET
        fetched = await skill_service.get_skill(skill_id)
        assert fetched.status == SkillStatus.ARCHIVED

        # GET revisions / revision detail
        revisions = await skill_service.list_revisions(skill_id, limit=50, offset=0)
        assert revisions.total == 1
        detail = await skill_service.get_revision(skill_id, 1)
        assert detail.revision == 1

        # POST revision while archived
        new_rev, created_flag = await skill_service.create_revision(
            skill_id, build_revision_request(description="v2 while archived")
        )
        assert created_flag is True
        assert new_rev.revision == 2

        still_archived = await skill_service.get_skill(skill_id)
        assert still_archived.status == SkillStatus.ARCHIVED
        assert still_archived.current_revision == 2

        # PATCH current_revision while archived
        rolled_back = await skill_service.update_skill(
            skill_id, SkillUpdateRequest(current_revision=1)
        )
        assert rolled_back.status == SkillStatus.ARCHIVED
        assert rolled_back.current_revision == 1

        # archive again -- no-op
        archived_again = await skill_service.archive_skill(skill_id)
        assert archived_again.status == SkillStatus.ARCHIVED
        assert archived_again.archived_at == archived.archived_at
        assert archived_again.updated_at == rolled_back.updated_at  # untouched by the no-op

        # restore -- preserves current_revision at restore time (1, not 2)
        restored = await skill_service.restore_skill(skill_id)
        assert restored.status == SkillStatus.ACTIVE
        assert restored.archived_at is None
        assert restored.current_revision == 1

        # restore again -- no-op
        restored_again = await skill_service.restore_skill(skill_id)
        assert restored_again.status == SkillStatus.ACTIVE
        assert restored_again.updated_at == restored.updated_at
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_current_equals_two_create_revision_three_restore_keeps_three(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request(description="v1"))
    skill_id = created.skill_uuid

    try:
        rev2, _ = await skill_service.create_revision(
            skill_id, build_revision_request(description="v2")
        )
        assert rev2.revision == 2

        skill = await skill_service.get_skill(skill_id)
        assert skill.current_revision == 2

        await skill_service.archive_skill(skill_id)

        rev3, _ = await skill_service.create_revision(
            skill_id, build_revision_request(description="v3")
        )
        assert rev3.revision == 3

        archived_skill = await skill_service.get_skill(skill_id)
        assert archived_skill.current_revision == 3
        assert archived_skill.status == SkillStatus.ARCHIVED

        restored = await skill_service.restore_skill(skill_id)
        assert restored.current_revision == 3  # never reset to 2
        assert restored.status == SkillStatus.ACTIVE
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


# --- timestamp no-op discipline ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_to_same_current_revision_is_idempotent_no_timestamp_churn(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request())
    skill_id = created.skill_uuid

    try:
        result = await skill_service.update_skill(skill_id, SkillUpdateRequest(current_revision=1))
        assert result.current_revision == 1
        assert result.updated_at == created.updated_at
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_to_different_revision_bumps_updated_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request())
    skill_id = created.skill_uuid

    try:
        await skill_service.create_revision(skill_id, build_revision_request(description="v2"))
        rolled_back = await skill_service.update_skill(
            skill_id, SkillUpdateRequest(current_revision=1)
        )
        assert rolled_back.updated_at > created.updated_at
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


# --- rollback (PATCH) vs new-revision concurrency, both orders --------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rollback_wins_lock_first_new_revision_becomes_current(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request(description="v1"))
    skill_id = created.skill_uuid

    try:
        await skill_service.create_revision(skill_id, build_revision_request(description="v2"))
        skill = await skill_service.get_skill(skill_id)
        assert skill.current_revision == 2

        rollback_started = asyncio.Event()
        rollback_may_finish = asyncio.Event()

        async def rollback_holding_lock() -> None:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                locked_skill = await uow.skills.get_by_id_for_update(skill_id)
                assert locked_skill is not None
                rollback_started.set()
                await rollback_may_finish.wait()
                target = await uow.skill_revisions.get_by_skill_and_revision(skill_id, 1)
                assert target is not None
                locked_skill.current_revision_id = target.id
                await uow.commit()

        rollback_task = asyncio.create_task(rollback_holding_lock())
        await asyncio.wait_for(rollback_started.wait(), timeout=5)

        # New-revision creation attempts to acquire the same lock second --
        # it must wait until the rollback task above releases it.
        new_revision_task = asyncio.create_task(
            skill_service.create_revision(skill_id, build_revision_request(description="v3"))
        )
        await asyncio.sleep(0.2)  # give it a chance to block on the lock
        assert not new_revision_task.done()

        rollback_may_finish.set()
        await asyncio.wait_for(rollback_task, timeout=5)
        result, created_flag = await asyncio.wait_for(new_revision_task, timeout=5)
        assert created_flag is True
        assert result.revision == 3

        final = await skill_service.get_skill(skill_id)
        assert final.current_revision == 3  # the creation, which acquired the lock second, wins
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_revision_wins_lock_first_rollback_target_becomes_current(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request(description="v1"))
    skill_id = created.skill_uuid

    try:
        await skill_service.create_revision(skill_id, build_revision_request(description="v2"))

        creation_started = asyncio.Event()
        creation_may_finish = asyncio.Event()

        async def create_holding_lock() -> None:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                locked_skill = await uow.skills.get_by_id_for_update(skill_id)
                assert locked_skill is not None
                creation_started.set()
                await creation_may_finish.wait()
                next_number = await uow.skill_revisions.next_revision_number(skill_id)
                from sofias_memory.infrastructure.postgres.models import SkillRevision

                new_revision = SkillRevision(
                    skill_id=skill_id,
                    revision=next_number,
                    description="v3-manual",
                    procedure="p",
                    metadata_={},
                    tags=[],
                    declared_tools=[],
                    content_sha256="d" * 64,
                    resolution_embedding=[0.0] * EMBEDDING_DIMENSIONS,
                )
                await uow.skill_revisions.add(new_revision)
                locked_skill.current_revision_id = new_revision.id
                await uow.commit()

        creation_task = asyncio.create_task(create_holding_lock())
        await asyncio.wait_for(creation_started.wait(), timeout=5)

        rollback_task = asyncio.create_task(
            skill_service.update_skill(skill_id, SkillUpdateRequest(current_revision=1))
        )
        await asyncio.sleep(0.2)
        assert not rollback_task.done()

        creation_may_finish.set()
        await asyncio.wait_for(creation_task, timeout=5)
        rollback_result = await asyncio.wait_for(rollback_task, timeout=5)

        # rollback, which acquired the lock second, wins
        assert rollback_result.current_revision == 1

        final = await skill_service.get_skill(skill_id)
        assert final.current_revision == 1
        revisions = await skill_service.list_revisions(skill_id, limit=50, offset=0)
        assert revisions.total == 3  # v1, v2, v3-manual all still exist
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})


# --- duplicate-name creation concurrency ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_create_skill_same_name_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm702-name-race")

    async def attempt(index: int) -> object:
        try:
            return await service(postgres_session_factory, FakeEmbeddingClient()).create_skill(
                build_create_request(name=name, description=f"candidate {index}")
            )
        except SofiasMemoryError as exc:
            return exc

    results = await asyncio.gather(*(attempt(i) for i in range(5)))

    from sofias_memory.schemas.skills import SkillResult

    winners = [r for r in results if isinstance(r, SkillResult)]
    conflicts = [r for r in results if isinstance(r, SofiasMemoryError)]

    try:
        assert len(winners) == 1
        assert len(conflicts) == 4
        assert all(exc.status_code == 409 for exc in conflicts)
        assert all(exc.code.value == "INVALID_REQUEST" for exc in conflicts)
    finally:
        await cleanup_skills(postgres_session_factory, {winners[0].skill_uuid})


# --- not-found / target-missing error mapping -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_missing_skill_raises_404_invalid_request(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    with pytest.raises(SofiasMemoryError) as exc_info:
        await service(postgres_session_factory, FakeEmbeddingClient()).get_skill(uuid4())
    assert exc_info.value.status_code == 404
    assert exc_info.value.code.value == "INVALID_REQUEST"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_revision_on_missing_skill_raises_404(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    with pytest.raises(SofiasMemoryError) as exc_info:
        await service(postgres_session_factory, FakeEmbeddingClient()).create_revision(
            uuid4(), build_revision_request()
        )
    assert exc_info.value.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_missing_revision_target_raises_422(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    skill_service = service(postgres_session_factory, FakeEmbeddingClient())
    created = await skill_service.create_skill(build_create_request())
    skill_id = created.skill_uuid

    try:
        with pytest.raises(SofiasMemoryError) as exc_info:
            await skill_service.update_skill(skill_id, SkillUpdateRequest(current_revision=99))
        assert exc_info.value.status_code == 422
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id})
