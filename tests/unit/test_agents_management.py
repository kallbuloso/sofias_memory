from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import (
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_DISPLAY_NAME_MAX_LENGTH,
    AGENT_INSTRUCTIONS_MAX_LENGTH,
    AGENT_NAME_MAX_LENGTH,
    AgentStatus,
)
from sofias_memory.infrastructure.postgres.models import Agent
from sofias_memory.schemas.agents import AgentCreateRequest, AgentUpdateRequest
from sofias_memory.services.agents import (
    AGENT_NAME_UNIQUE_CONSTRAINT,
    AgentService,
    AgentUnitOfWork,
    UnitOfWorkFactory,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def fake_integrity_error(constraint_name: str | None) -> IntegrityError:
    """Builds an ``IntegrityError`` whose ``.orig`` carries a
    ``constraint_name`` attribute, the same shape asyncpg's real
    ``UniqueViolationError`` exposes -- mirrors
    ``tests/unit/test_pipeline_submission.py.fake_integrity_error``."""

    class _FakeOrig(Exception):
        def __init__(self, name: str | None) -> None:
            super().__init__("simulated constraint violation")
            self.constraint_name = name

    return IntegrityError("insert", {}, _FakeOrig(constraint_name))


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_create_request_minimal_defaults() -> None:
    request = AgentCreateRequest(name="research-agent")
    assert request.display_name is None
    assert request.description is None
    assert request.instructions is None
    assert request.metadata == {}


def test_create_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="research-agent", status="archived")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="research-agent", agent_uuid=str(uuid4()))  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentCreateRequest(  # type: ignore[call-arg]
            name="research-agent", created_at="2026-01-01T00:00:00Z"
        )


@pytest.mark.parametrize(
    "name",
    ["a", "agent", "research-agent", "a" * AGENT_NAME_MAX_LENGTH],
)
def test_create_request_accepts_valid_names(name: str) -> None:
    assert AgentCreateRequest(name=name).name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Agent",
        "-agent",
        "agent-",
        "agent--worker",
        "agent_worker",
        "agent worker",
        "a" * (AGENT_NAME_MAX_LENGTH + 1),
    ],
)
def test_create_request_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        AgentCreateRequest(name=name)


def test_create_request_display_name_trim_and_boundaries() -> None:
    assert AgentCreateRequest(name="a", display_name="  Research  ").display_name == "Research"
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", display_name="   ")
    at_max = "x" * AGENT_DISPLAY_NAME_MAX_LENGTH
    assert AgentCreateRequest(name="a", display_name=at_max).display_name == at_max
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", display_name="x" * (AGENT_DISPLAY_NAME_MAX_LENGTH + 1))
    # Padded-then-trimmed must still be accepted -- the raw padded length
    # exceeds the max, but the trimmed length does not (proves no premature
    # Pydantic max_length check runs on the pre-trim value).
    padded = " " + ("y" * AGENT_DISPLAY_NAME_MAX_LENGTH) + " "
    assert AgentCreateRequest(name="a", display_name=padded).display_name == (
        "y" * AGENT_DISPLAY_NAME_MAX_LENGTH
    )


def test_create_request_description_boundaries_no_trim() -> None:
    assert AgentCreateRequest(name="a", description="x").description == "x"
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", description="")
    at_max = "x" * AGENT_DESCRIPTION_MAX_LENGTH
    assert AgentCreateRequest(name="a", description=at_max).description == at_max
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", description="x" * (AGENT_DESCRIPTION_MAX_LENGTH + 1))
    # SM-801 decision preserved: whitespace-only within bounds is accepted,
    # no invented nonblank rule.
    assert AgentCreateRequest(name="a", description="   ").description == "   "


def test_create_request_instructions_normalization_and_boundaries() -> None:
    normalized = AgentCreateRequest(name="a", instructions="line1\r\nline2\rline3").instructions
    assert normalized == "line1\nline2\nline3"
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", instructions="   \n\t ")
    at_max = "x" * AGENT_INSTRUCTIONS_MAX_LENGTH
    assert AgentCreateRequest(name="a", instructions=at_max).instructions == at_max
    with pytest.raises(ValidationError):
        AgentCreateRequest(name="a", instructions="x" * (AGENT_INSTRUCTIONS_MAX_LENGTH + 1))


def test_create_request_metadata_accepts_nested_json_shapes() -> None:
    payload = {"nested": {"x": 1}, "array": [1, True, None], "number": 42}
    request = AgentCreateRequest(name="a", metadata=payload)
    assert request.metadata == payload


def test_update_request_rejects_empty_patch() -> None:
    with pytest.raises(ValidationError):
        AgentUpdateRequest()


def test_update_request_rejects_null_metadata() -> None:
    with pytest.raises(ValidationError):
        AgentUpdateRequest(metadata=None, display_name="kept")  # type: ignore[arg-type]


def test_update_request_allows_explicit_null_fields_to_clear() -> None:
    request = AgentUpdateRequest(display_name=None, description=None, instructions=None)
    assert {"display_name", "description", "instructions"} <= request.model_fields_set
    assert request.display_name is None
    assert request.description is None
    assert request.instructions is None


def test_update_request_rejects_forbidden_fields() -> None:
    with pytest.raises(ValidationError):
        AgentUpdateRequest(name="new-name")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentUpdateRequest(status="archived")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentUpdateRequest(agent_uuid=str(uuid4()))  # type: ignore[call-arg]


def test_update_request_metadata_wholesale_replace_distinguishable_from_omitted() -> None:
    unset = AgentUpdateRequest(display_name="x")
    assert "metadata" not in unset.model_fields_set

    replaced = AgentUpdateRequest(metadata={"c": 3})
    assert "metadata" in replaced.model_fields_set
    assert replaced.metadata == {"c": 3}


# ---------------------------------------------------------------------------
# Service (fake in-memory Unit of Work)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.agents: list[Agent] = []
        self.commits = 0


class FakeAgentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.get_by_id_calls: list[UUID] = []
        self.get_by_id_for_update_calls: list[UUID] = []

    async def add(self, agent: Agent) -> Agent:
        if any(existing.name == agent.name for existing in self._store.agents):
            raise fake_integrity_error(AGENT_NAME_UNIQUE_CONSTRAINT)
        self._store.agents.append(agent)
        return agent

    async def get_by_id(self, agent_id: UUID) -> Agent | None:
        self.get_by_id_calls.append(agent_id)
        return next((a for a in self._store.agents if a.id == agent_id), None)

    async def get_by_id_for_update(self, agent_id: UUID) -> Agent | None:
        self.get_by_id_for_update_calls.append(agent_id)
        return next((a for a in self._store.agents if a.id == agent_id), None)

    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: AgentStatus,
    ) -> tuple[list[Agent], int]:
        items = [a for a in self._store.agents if a.status == status]
        ordered = sorted(items, key=lambda a: (a.created_at, a.id))
        return ordered[offset : offset + limit], len(ordered)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore, repository: FakeAgentRepository) -> None:
        self._store = store
        self.agents = repository

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(
    store: FakeStore, repository: FakeAgentRepository | None = None
) -> tuple[AgentService, FakeAgentRepository]:
    repo = repository or FakeAgentRepository(store)

    def create_uow() -> AgentUnitOfWork:
        return cast(AgentUnitOfWork, FakeUnitOfWork(store, repo))

    return AgentService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow)), repo


def make_agent(
    *,
    name: str,
    agent_id: UUID | None = None,
    display_name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: AgentStatus = AgentStatus.ACTIVE,
    created_at: datetime = CREATED_AT,
    archived_at: datetime | None = None,
) -> Agent:
    return Agent(
        id=agent_id or uuid4(),
        name=name,
        display_name=display_name,
        description=description,
        instructions=instructions,
        metadata_=metadata or {},
        status=status,
        created_at=created_at,
        updated_at=created_at,
        archived_at=archived_at,
    )


@pytest.mark.asyncio
async def test_create_agent_success() -> None:
    store = FakeStore()
    service, _ = service_for(store)

    result = await service.create_agent(AgentCreateRequest(name="research-agent"))

    assert result.name == "research-agent"
    assert result.status == AgentStatus.ACTIVE
    assert result.metadata == {}
    assert result.created_at == result.updated_at
    assert result.archived_at is None
    assert store.commits == 1


@pytest.mark.asyncio
async def test_create_agent_duplicate_name_is_conflict_not_upsert() -> None:
    store = FakeStore()
    service, _ = service_for(store)
    await service.create_agent(AgentCreateRequest(name="dup", description="First."))

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.create_agent(AgentCreateRequest(name="dup", description="Second."))

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "INVALID_REQUEST"
    assert len(store.agents) == 1
    assert store.agents[0].description == "First."


@pytest.mark.asyncio
async def test_create_agent_unrelated_integrity_error_propagates_unchanged() -> None:
    """Only `uq_agents_name` is ever reinterpreted as a name conflict -- any
    other constraint violation must propagate as a plain `IntegrityError`,
    never swallowed or misreported as a 409."""

    store = FakeStore()
    repository = FakeAgentRepository(store)

    async def add_raises_unrelated(agent: Agent) -> Agent:
        del agent
        raise fake_integrity_error("some_other_constraint")

    repository.add = add_raises_unrelated  # type: ignore[method-assign]
    service, _ = service_for(store, repository)

    with pytest.raises(IntegrityError):
        await service.create_agent(AgentCreateRequest(name="a"))


@pytest.mark.asyncio
async def test_get_agent_existing_and_missing() -> None:
    store = FakeStore()
    agent = make_agent(name="a")
    store.agents.append(agent)
    service, _ = service_for(store)

    found = await service.get_agent(agent.id)
    assert found.agent_uuid == agent.id

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.get_agent(uuid4())
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_list_agents_default_status_forwarding_and_ordering() -> None:
    store = FakeStore()
    older = make_agent(name="older", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = make_agent(name="newer", created_at=datetime(2026, 1, 2, tzinfo=UTC))
    archived = make_agent(
        name="archived-one",
        status=AgentStatus.ARCHIVED,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    store.agents.extend([newer, older, archived])
    service, _ = service_for(store)

    active_only = await service.list_agents(limit=50, offset=0, status=AgentStatus.ACTIVE)
    archived_only = await service.list_agents(limit=50, offset=0, status=AgentStatus.ARCHIVED)

    assert [item.name for item in active_only.items] == ["older", "newer"]
    assert active_only.total == 2
    assert [item.name for item in archived_only.items] == ["archived-one"]
    assert archived_only.total == 1


@pytest.mark.asyncio
async def test_list_agents_item_never_includes_instructions_or_metadata() -> None:
    store = FakeStore()
    store.agents.append(
        make_agent(name="a", instructions="secret procedure", metadata={"secret": "value"})
    )
    service, _ = service_for(store)

    listed = await service.list_agents(limit=50, offset=0, status=AgentStatus.ACTIVE)

    item_dump = listed.items[0].model_dump()
    assert "instructions" not in item_dump
    assert "metadata" not in item_dump


@pytest.mark.asyncio
async def test_update_agent_fields_and_metadata_wholesale_replace() -> None:
    store = FakeStore()
    agent = make_agent(name="a", display_name="Old", metadata={"a": 1, "b": 2})
    store.agents.append(agent)
    service, _ = service_for(store)

    updated = await service.update_agent(
        agent.id, AgentUpdateRequest(display_name="New", metadata={"c": 3})
    )

    assert updated.display_name == "New"
    assert updated.metadata == {"c": 3}  # wholesale replace, never merged
    assert updated.updated_at > CREATED_AT


@pytest.mark.asyncio
async def test_update_agent_null_clears_text_fields() -> None:
    store = FakeStore()
    agent = make_agent(
        name="a", display_name="X", description="Y", instructions="Z", metadata={"k": "v"}
    )
    store.agents.append(agent)
    service, _ = service_for(store)

    cleared = await service.update_agent(
        agent.id,
        AgentUpdateRequest(display_name=None, description=None, instructions=None),
    )

    assert cleared.display_name is None
    assert cleared.description is None
    assert cleared.instructions is None
    assert cleared.metadata == {"k": "v"}  # metadata untouched -- not in fields_set


@pytest.mark.asyncio
async def test_update_agent_updated_at_advances_even_when_value_unchanged() -> None:
    """SS 18: a syntactically valid PATCH is a management mutation request;
    updated_at advances even if the assigned value equals the prior one."""

    store = FakeStore()
    agent = make_agent(name="a", display_name="Same")
    store.agents.append(agent)
    service, _ = service_for(store)

    result = await service.update_agent(agent.id, AgentUpdateRequest(display_name="Same"))

    assert result.display_name == "Same"
    assert result.updated_at > CREATED_AT


@pytest.mark.asyncio
async def test_update_agent_permitted_while_archived() -> None:
    store = FakeStore()
    agent = make_agent(name="a", status=AgentStatus.ARCHIVED, archived_at=CREATED_AT)
    store.agents.append(agent)
    service, _ = service_for(store)

    updated = await service.update_agent(agent.id, AgentUpdateRequest(display_name="New"))

    assert updated.display_name == "New"
    assert updated.status == AgentStatus.ARCHIVED
    assert updated.archived_at == CREATED_AT


@pytest.mark.asyncio
async def test_update_agent_missing_is_404() -> None:
    store = FakeStore()
    service, _ = service_for(store)
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.update_agent(uuid4(), AgentUpdateRequest(display_name="X"))
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_archive_agent_transition_and_exact_replay() -> None:
    store = FakeStore()
    agent = make_agent(name="a")
    store.agents.append(agent)
    service, _ = service_for(store)

    first = await service.archive_agent(agent.id)
    assert first.status == AgentStatus.ARCHIVED
    assert first.archived_at is not None
    assert first.updated_at == first.archived_at

    replay = await service.archive_agent(agent.id)
    assert replay.status == AgentStatus.ARCHIVED
    assert replay.archived_at == first.archived_at
    assert replay.updated_at == first.updated_at


@pytest.mark.asyncio
async def test_restore_agent_transition_and_exact_replay() -> None:
    store = FakeStore()
    agent = make_agent(name="a", status=AgentStatus.ARCHIVED, archived_at=CREATED_AT)
    store.agents.append(agent)
    service, _ = service_for(store)

    first = await service.restore_agent(agent.id)
    assert first.status == AgentStatus.ACTIVE
    assert first.archived_at is None
    assert first.updated_at > CREATED_AT

    replay = await service.restore_agent(agent.id)
    assert replay.status == AgentStatus.ACTIVE
    assert replay.archived_at is None
    assert replay.updated_at == first.updated_at


@pytest.mark.asyncio
async def test_mutators_use_row_lock_read_never_detail_or_list() -> None:
    store = FakeStore()
    agent = make_agent(name="a")
    store.agents.append(agent)
    service, repository = service_for(store)

    await service.get_agent(agent.id)
    await service.list_agents(limit=50, offset=0, status=AgentStatus.ACTIVE)
    assert repository.get_by_id_for_update_calls == []
    assert repository.get_by_id_calls == [agent.id]

    await service.update_agent(agent.id, AgentUpdateRequest(display_name="X"))
    await service.archive_agent(agent.id)
    await service.restore_agent(agent.id)
    assert repository.get_by_id_for_update_calls == [agent.id, agent.id, agent.id]
