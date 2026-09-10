from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import AgentStatus, SkillStatus
from sofias_memory.infrastructure.postgres.models import Agent, Skill, SkillRevision
from sofias_memory.infrastructure.postgres.repositories.agent_skills import AgentSkillRow
from sofias_memory.schemas.agent_skills import AgentSkillSetRequest
from sofias_memory.services.agent_skills import (
    AgentSkillService,
    AgentSkillUnitOfWork,
    UnitOfWorkFactory,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
FAKE_EMBEDDING = [0.0]

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_set_request_omitted_defaults_to_none() -> None:
    assert AgentSkillSetRequest().pinned_revision is None


def test_set_request_explicit_null_equals_omitted() -> None:
    assert AgentSkillSetRequest(pinned_revision=None).pinned_revision is None


def test_set_request_accepts_positive_integer() -> None:
    assert AgentSkillSetRequest(pinned_revision=3).pinned_revision == 3


def test_set_request_rejects_non_positive_revision() -> None:
    with pytest.raises(ValidationError):
        AgentSkillSetRequest(pinned_revision=0)
    with pytest.raises(ValidationError):
        AgentSkillSetRequest(pinned_revision=-1)


def test_set_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentSkillSetRequest(pinned_revision_id=str(uuid4()))  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentSkillSetRequest(effective_revision=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentSkillSetRequest(current_revision=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Service (fake in-memory Unit of Work)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.agents: dict[UUID, Agent] = {}
        self.skills: dict[UUID, Skill] = {}
        self.skill_revisions: dict[UUID, SkillRevision] = {}
        self.agent_skills: dict[tuple[UUID, UUID], dict[str, object]] = {}
        self.commits = 0


class FakeAgentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, agent_id: UUID) -> Agent | None:
        return self._store.agents.get(agent_id)


class FakeSkillRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, skill_id: UUID) -> Skill | None:
        return self._store.skills.get(skill_id)


class FakeSkillRevisionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_skill_and_revision(
        self, skill_id: UUID, revision: int
    ) -> SkillRevision | None:
        for candidate in self._store.skill_revisions.values():
            if candidate.skill_id == skill_id and candidate.revision == revision:
                return candidate
        return None


class FakeAgentSkillRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.set_pin_calls = 0
        self.delete_calls = 0

    async def set_pin(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
        pinned_revision_id: UUID | None,
    ) -> None:
        self.set_pin_calls += 1
        key = (agent_id, skill_id)
        existing = self._store.agent_skills.get(key)
        if existing is None:
            self._store.agent_skills[key] = {
                "pinned_revision_id": pinned_revision_id,
                "created_at": CREATED_AT,
            }
        else:
            existing["pinned_revision_id"] = pinned_revision_id  # created_at untouched

    async def delete(self, *, agent_id: UUID, skill_id: UUID) -> None:
        self.delete_calls += 1
        self._store.agent_skills.pop((agent_id, skill_id), None)

    async def get_one_for_agent_and_skill(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
    ) -> AgentSkillRow | None:
        entry = self._store.agent_skills.get((agent_id, skill_id))
        if entry is None:
            return None
        return self._row(agent_id, skill_id, entry)

    async def list_for_agent(self, agent_id: UUID) -> list[AgentSkillRow]:
        rows = [
            self._row(a_id, s_id, entry)
            for (a_id, s_id), entry in self._store.agent_skills.items()
            if a_id == agent_id
        ]
        rows.sort(key=lambda row: (row.association_created_at, row.skill_uuid))
        return rows

    def _row(self, agent_id: UUID, skill_id: UUID, entry: dict[str, object]) -> AgentSkillRow:
        del agent_id
        skill = self._store.skills[skill_id]
        current_revision = self._store.skill_revisions[skill.current_revision_id]
        pinned_revision_id = cast(UUID | None, entry["pinned_revision_id"])
        if pinned_revision_id is not None:
            effective_revision = self._store.skill_revisions[pinned_revision_id]
            pinned_revision_number: int | None = effective_revision.revision
        else:
            effective_revision = current_revision
            pinned_revision_number = None
        return AgentSkillRow(
            skill_uuid=skill_id,
            name=skill.name,
            status=skill.status,
            current_revision=current_revision.revision,
            pinned_revision=pinned_revision_number,
            effective_revision=effective_revision.revision,
            description=effective_revision.description,
            tags=effective_revision.tags,
            declared_tools=effective_revision.declared_tools,
            compatibility=effective_revision.compatibility,
            association_created_at=cast(datetime, entry["created_at"]),
        )


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.agents = FakeAgentRepository(store)
        self.skills = FakeSkillRepository(store)
        self.skill_revisions = FakeSkillRevisionRepository(store)
        self.agent_skills = FakeAgentSkillRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> AgentSkillService:
    def create_uow() -> AgentSkillUnitOfWork:
        return cast(AgentSkillUnitOfWork, FakeUnitOfWork(store))

    return AgentSkillService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def make_agent(*, status: AgentStatus = AgentStatus.ACTIVE) -> Agent:
    return Agent(
        id=uuid4(),
        name=f"agent-{uuid4().hex[:8]}",
        display_name=None,
        description=None,
        instructions=None,
        metadata_={},
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        archived_at=None,
    )


def make_skill_with_revisions(
    *,
    revision_count: int = 1,
    current_revision_index: int = 0,
    status: SkillStatus = SkillStatus.ACTIVE,
) -> tuple[Skill, list[SkillRevision]]:
    skill_id = uuid4()
    revisions = [
        SkillRevision(
            id=uuid4(),
            skill_id=skill_id,
            revision=index + 1,
            description=f"Revision {index + 1}.",
            procedure="Do the thing.",
            license=None,
            compatibility=None,
            metadata_={},
            tags=[],
            declared_tools=[],
            content_sha256="a" * 64,
            resolution_embedding=FAKE_EMBEDDING,
            created_at=CREATED_AT,
        )
        for index in range(revision_count)
    ]
    skill = Skill(
        id=skill_id,
        name=f"skill-{uuid4().hex[:8]}",
        status=status,
        current_revision_id=revisions[current_revision_index].id,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        archived_at=None,
    )
    return skill, revisions


def seed(store: FakeStore, *, agent: Agent, skill: Skill, revisions: list[SkillRevision]) -> None:
    store.agents[agent.id] = agent
    store.skills[skill.id] = skill
    for revision in revisions:
        store.skill_revisions[revision.id] = revision


# --- Agent/Skill existence -----------------------------------------------


@pytest.mark.asyncio
async def test_set_skill_agent_missing_is_404() -> None:
    store = FakeStore()
    skill, revisions = make_skill_with_revisions()
    store.skills[skill.id] = skill
    for revision in revisions:
        store.skill_revisions[revision.id] = revision
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.set_skill(uuid4(), skill.id, AgentSkillSetRequest())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_set_skill_skill_missing_is_404() -> None:
    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.set_skill(agent.id, uuid4(), AgentSkillSetRequest())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_list_skills_agent_missing_is_404() -> None:
    store = FakeStore()
    service = service_for(store)
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.list_skills(uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_remove_skill_agent_missing_is_404() -> None:
    store = FakeStore()
    service = service_for(store)
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.remove_skill(uuid4(), uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_remove_skill_skill_missing_is_404_not_204() -> None:
    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.remove_skill(agent.id, uuid4())
    assert excinfo.value.status_code == 404


# --- follow-current / pin --------------------------------------------------


@pytest.mark.asyncio
async def test_set_skill_follow_current_when_omitted() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions()
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    result = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

    assert result.pinned_revision is None
    assert result.current_revision == 1
    assert result.effective_revision == 1


@pytest.mark.asyncio
async def test_set_skill_pin_resolves_via_skill_id_and_revision() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions(revision_count=3, current_revision_index=2)
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    result = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))

    assert result.pinned_revision == 1
    assert result.current_revision == 3
    assert result.effective_revision == 1
    assert result.description == revisions[0].description


@pytest.mark.asyncio
async def test_set_skill_invalid_pinned_revision_is_422() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions()
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=99))
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_set_skill_cross_skill_revision_number_is_invalid() -> None:
    """Revision 2 exists on Skill B but not Skill A -- lookup must be
    (skill_id, revision), never a global revision number."""

    store = FakeStore()
    agent = make_agent()
    skill_a, revisions_a = make_skill_with_revisions(revision_count=1)
    skill_b, revisions_b = make_skill_with_revisions(revision_count=2)
    seed(store, agent=agent, skill=skill_a, revisions=revisions_a)
    store.skills[skill_b.id] = skill_b
    for revision in revisions_b:
        store.skill_revisions[revision.id] = revision
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.set_skill(agent.id, skill_a.id, AgentSkillSetRequest(pinned_revision=2))
    assert excinfo.value.status_code == 422


# --- upsert / re-pin / unpin -----------------------------------------------


@pytest.mark.asyncio
async def test_set_skill_upsert_creates_then_updates_same_row() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions(revision_count=2)
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
    await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=2))

    assert len(store.agent_skills) == 1


@pytest.mark.asyncio
async def test_set_skill_re_pin_preserves_created_at() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions(revision_count=2)
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    first = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
    second = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=2))

    assert second.pinned_revision == 2
    assert second.association_created_at == first.association_created_at


@pytest.mark.asyncio
async def test_set_skill_unpin_preserves_created_at() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions(revision_count=2)
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    pinned = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
    unpinned = await service.set_skill(
        agent.id, skill.id, AgentSkillSetRequest(pinned_revision=None)
    )

    assert unpinned.pinned_revision is None
    assert unpinned.effective_revision == unpinned.current_revision
    assert unpinned.association_created_at == pinned.association_created_at


# --- DELETE -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_skill_existing_association() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions()
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)
    await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())
    assert len(store.agent_skills) == 1

    await service.remove_skill(agent.id, skill.id)

    assert len(store.agent_skills) == 0


@pytest.mark.asyncio
async def test_remove_skill_missing_association_is_idempotent_noop() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions()
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    await service.remove_skill(agent.id, skill.id)  # no association ever created
    await service.remove_skill(agent.id, skill.id)  # replay

    assert len(store.agent_skills) == 0


# --- archived Agent / archived Skill -----------------------------------------


@pytest.mark.asyncio
async def test_set_skill_archived_agent_accepted() -> None:
    store = FakeStore()
    agent = make_agent(status=AgentStatus.ARCHIVED)
    skill, revisions = make_skill_with_revisions()
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    result = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

    assert result.skill_uuid == skill.id


@pytest.mark.asyncio
async def test_set_skill_archived_skill_accepted_and_visible_in_list() -> None:
    store = FakeStore()
    agent = make_agent()
    skill, revisions = make_skill_with_revisions(status=SkillStatus.ARCHIVED)
    seed(store, agent=agent, skill=skill, revisions=revisions)
    service = service_for(store)

    await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())
    listed = await service.list_skills(agent.id)

    assert len(listed.items) == 1
    assert listed.items[0].status == SkillStatus.ARCHIVED


@pytest.mark.asyncio
async def test_list_skills_empty_for_agent_with_no_associations() -> None:
    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    result = await service.list_skills(agent.id)

    assert result.items == []
