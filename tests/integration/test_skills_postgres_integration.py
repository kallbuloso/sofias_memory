"""Real-PostgreSQL tests for the SM-701 Skills persistence foundation.

Proves ADR-0013 / Feature Contract v0.4.0 Skills: the single-UUID identity
(no ``skill_uuid`` column), the composite ``DEFERRABLE INITIALLY DEFERRED``
current-revision foreign key (cross-Skill pointer rejection), initial
aggregate atomicity, revision-creation concurrency (different content, same
content, historical replay), and Skill name creation concurrency. Requires
migrations already applied through 0014 against the configured PostgreSQL
database. No public route, no embedding provider call, no Neo4j, and no
``graph_outbox`` involvement anywhere in this suite.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from sofias_memory.config import load_settings
from sofias_memory.domain import SOFIAS_MEMORY_TAGS_METADATA_KEY, SkillRevisionContent, SkillStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.services.skills import (
    SkillNameAlreadyExistsError,
    create_skill_aggregate,
    create_skill_revision,
)

POSTGRES_SKILLS_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_SKILLS_TESTS"

FAKE_EMBEDDING = [0.0] * 3072


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SKILLS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_ENV}=1 to run PostgreSQL Skills tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def build_content(**overrides: object) -> SkillRevisionContent:
    base: dict[str, object] = {
        "description": "A description.",
        "procedure": "Do the thing.",
        "license": None,
        "compatibility": None,
        "metadata": {},
        "tags": [],
        "declared_tools": [],
    }
    base.update(overrides)
    return SkillRevisionContent(**base)  # type: ignore[arg-type]


async def cleanup_skills(session_factory: AsyncSessionFactory, *, skill_ids: set[UUID]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


# --- initial aggregate atomicity -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_skill_aggregate_success_commits_skill_and_revision_one(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-aggregate")

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            skill = await create_skill_aggregate(
                uow,
                name=name,
                content=build_content(),
                resolution_embedding=FAKE_EMBEDDING,
            )
            await uow.commit()
            skill_id = skill.id

        async with postgres_session_factory() as session:
            persisted_skill = await session.get(Skill, skill_id)
            assert persisted_skill is not None
            assert persisted_skill.name == name
            assert persisted_skill.status == SkillStatus.ACTIVE
            assert persisted_skill.current_revision_id is not None

            revisions = list(
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            )
            assert len(revisions) == 1
            assert revisions[0].revision == 1
            assert revisions[0].id == persisted_skill.current_revision_id
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={skill_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_skill_aggregate_never_leaves_partial_rows_without_commit(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-rollback")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        skill = await create_skill_aggregate(
            uow,
            name=name,
            content=build_content(),
            resolution_embedding=FAKE_EMBEDDING,
        )
        skill_id = skill.id
        # Deliberately never commit -- simulates a crash after the aggregate
        # was staged/flushed inside the SAVEPOINT but before the outer
        # transaction's commit. __aexit__ rolls back automatically.

    async with postgres_session_factory() as session:
        persisted_skill = await session.get(Skill, skill_id)
        assert persisted_skill is None

        revisions = list(
            await session.scalars(select(SkillRevision).where(SkillRevision.skill_id == skill_id))
        )
        assert revisions == []


# --- current_revision cross-Skill rejection --------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_current_revision_pointing_at_another_skills_revision_is_rejected(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name_a = unique_name("sm701-cross-a")
    name_b = unique_name("sm701-cross-b")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        skill_a = await create_skill_aggregate(
            uow, name=name_a, content=build_content(), resolution_embedding=FAKE_EMBEDDING
        )
        skill_b = await create_skill_aggregate(
            uow, name=name_b, content=build_content(), resolution_embedding=FAKE_EMBEDDING
        )
        await uow.commit()
        skill_a_id = skill_a.id
        skill_b_id = skill_b.id

    try:
        async with postgres_session_factory() as session:
            persisted_a = await session.get(Skill, skill_a_id)
            persisted_b = await session.get(Skill, skill_b_id)
            assert persisted_a is not None
            assert persisted_b is not None

            # Point Skill A's current_revision_id at Skill B's own revision.
            # The composite FK is DEFERRABLE INITIALLY DEFERRED, so this is
            # not caught at flush time -- only PostgreSQL's own commit-time
            # constraint check catches it, never a service-level 422.
            persisted_a.current_revision_id = persisted_b.current_revision_id
            await session.flush()  # succeeds: the check is deferred to commit

            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={skill_a_id, skill_b_id})


# --- revision concurrency: different content -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_revision_creation_different_content_gets_distinct_ordinals(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-concurrent-diff")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        skill = await create_skill_aggregate(
            uow, name=name, content=build_content(), resolution_embedding=FAKE_EMBEDDING
        )
        await uow.commit()
        skill_id = skill.id

    async def add_revision(index: int) -> tuple[int, UUID]:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            outcome = await create_skill_revision(
                uow,
                skill_id=skill_id,
                name=name,
                content=build_content(description=f"Different content {index}."),
                resolution_embedding=FAKE_EMBEDDING,
            )
            await uow.commit()
            return outcome.revision.revision, outcome.revision.id

    try:
        results = await asyncio.gather(*(add_revision(i) for i in range(5)))

        revision_numbers = [number for number, _ in results]
        assert sorted(revision_numbers) == [2, 3, 4, 5, 6]
        assert len(set(revision_numbers)) == 5  # no duplicates, no lost update

        max_number, max_revision_id = max(results, key=lambda item: item[0])

        async with postgres_session_factory() as session:
            persisted_skill = await session.get(Skill, skill_id)
            assert persisted_skill is not None
            # Serialized by the per-Skill lock: the last committer's
            # revision is deterministically the highest ordinal, and
            # current_revision_id deterministically matches it.
            assert persisted_skill.current_revision_id == max_revision_id

            revisions = list(
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            )
            assert len(revisions) == 6  # revision 1 (initial) + 5 concurrent
            assert max_number == max(r.revision for r in revisions)
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={skill_id})


# --- revision concurrency: same content -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_revision_creation_same_content_replays_to_one_revision(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-concurrent-same")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        skill = await create_skill_aggregate(
            uow, name=name, content=build_content(), resolution_embedding=FAKE_EMBEDDING
        )
        await uow.commit()
        skill_id = skill.id

    identical_content = build_content(description="Identical content for every writer.")

    async def add_revision() -> tuple[bool, UUID]:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            outcome = await create_skill_revision(
                uow,
                skill_id=skill_id,
                name=name,
                content=identical_content,
                resolution_embedding=FAKE_EMBEDDING,
            )
            await uow.commit()
            return outcome.created, outcome.revision.id

    try:
        results = await asyncio.gather(*(add_revision() for _ in range(5)))

        revision_ids = {revision_id for _, revision_id in results}
        assert len(revision_ids) == 1  # every writer resolved to the same revision

        created_flags = [created for created, _ in results]
        assert created_flags.count(True) == 1  # exactly one writer actually created it
        assert created_flags.count(False) == 4  # everyone else replayed

        async with postgres_session_factory() as session:
            revisions = list(
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            )
            assert len(revisions) == 2  # revision 1 (initial) + exactly one new revision

            persisted_skill = await session.get(Skill, skill_id)
            assert persisted_skill is not None
            assert persisted_skill.current_revision_id in revision_ids
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={skill_id})


# --- historical content replay: current unchanged --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replaying_a_historical_revisions_content_never_moves_current(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-historical-replay")
    content_a = build_content(description="Content A (revision 1).")
    content_b = build_content(description="Content B (revision 2).")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        skill = await create_skill_aggregate(
            uow, name=name, content=content_a, resolution_embedding=FAKE_EMBEDDING
        )
        skill_id = skill.id
        revision_1_id = skill.current_revision_id
        await uow.commit()

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        outcome_b = await create_skill_revision(
            uow,
            skill_id=skill_id,
            name=name,
            content=content_b,
            resolution_embedding=FAKE_EMBEDDING,
        )
        revision_2_id = outcome_b.revision.id
        await uow.commit()

    try:
        assert outcome_b.created is True
        assert revision_2_id != revision_1_id

        # Resubmit revision 1's exact content while current is revision 2.
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            replay_outcome = await create_skill_revision(
                uow,
                skill_id=skill_id,
                name=name,
                content=content_a,
                resolution_embedding=FAKE_EMBEDDING,
            )
            await uow.commit()

        assert replay_outcome.created is False
        assert replay_outcome.revision.id == revision_1_id
        assert replay_outcome.revision.revision == 1

        async with postgres_session_factory() as session:
            persisted_skill = await session.get(Skill, skill_id)
            assert persisted_skill is not None
            # current_revision_id is still revision 2 -- replaying an older
            # revision's content is never treated as a rollback.
            assert persisted_skill.current_revision_id == revision_2_id

            revisions = list(
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            )
            assert len(revisions) == 2  # no third revision was created by the replay
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={skill_id})


# --- Skill name creation concurrency ----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_skill_creation_with_same_name_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-name-race")

    async def attempt(index: int) -> Skill | SkillNameAlreadyExistsError:
        try:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                skill = await create_skill_aggregate(
                    uow,
                    name=name,
                    content=build_content(description=f"Candidate {index}."),
                    resolution_embedding=FAKE_EMBEDDING,
                )
                await uow.commit()
                return skill
        except SkillNameAlreadyExistsError as exc:
            return exc

    results = await asyncio.gather(*(attempt(i) for i in range(5)))

    winners = [r for r in results if isinstance(r, Skill)]
    conflicts = [r for r in results if isinstance(r, SkillNameAlreadyExistsError)]

    try:
        assert len(winners) == 1
        assert len(conflicts) == 4
        assert all(conflict.name == name for conflict in conflicts)

        async with postgres_session_factory() as session:
            skills_with_name = list(await session.scalars(select(Skill).where(Skill.name == name)))
            assert len(skills_with_name) == 1

            revisions = list(
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == winners[0].id)
                )
            )
            assert len(revisions) == 1
            assert revisions[0].revision == 1
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids={winners[0].id})


# --- metadata reserved-key DB defense ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_insert_with_reserved_tags_metadata_key_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm701-reserved-key")
    skill_id = uuid4()
    revision_id = uuid4()

    async with postgres_session_factory() as session:
        # Bypasses create_skill_aggregate/validate_metadata entirely -- this
        # proves the PostgreSQL CHECK constraint is the authoritative
        # defense, not merely a mirror of a domain-layer check that could be
        # skipped by a future direct-SQL caller.
        session.add(
            Skill(
                id=skill_id,
                name=name,
                status=SkillStatus.ACTIVE,
                current_revision_id=revision_id,
            )
        )
        session.add(
            SkillRevision(
                id=revision_id,
                skill_id=skill_id,
                revision=1,
                description="d",
                procedure="p",
                metadata_={SOFIAS_MEMORY_TAGS_METADATA_KEY: '["pdf"]'},
                tags=[],
                declared_tools=[],
                content_sha256="c" * 64,
                resolution_embedding=FAKE_EMBEDDING,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
