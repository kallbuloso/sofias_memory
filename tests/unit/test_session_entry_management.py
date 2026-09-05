from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres.models import Query, Session, SessionEntry
from sofias_memory.schemas.session_entries import (
    SESSION_ENTRY_CONTENT_MAX_LENGTH,
    SessionEntryCreateRequest,
    SessionEntryListResult,
    SessionEntryResult,
    SessionQueryListResult,
    SessionQuerySummaryResult,
)
from sofias_memory.services.session_entries import (
    SessionEntryService,
    SessionEntryUnitOfWork,
    UnitOfWorkFactory,
)
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_create_request_normalizes_external_id_and_preserves_case() -> None:
    request = SessionEntryCreateRequest(
        external_id="  Caller-Stable-ID  ", role="user", content="hi"
    )
    assert request.external_id == "Caller-Stable-ID"


def test_create_request_rejects_blank_external_id() -> None:
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(external_id="   ", role="user", content="hi")


def test_create_request_rejects_over_255_char_external_id() -> None:
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(external_id="a" * 256, role="user", content="hi")


def test_create_request_external_id_omitted_defaults_to_none() -> None:
    request = SessionEntryCreateRequest(role="user", content="hi")
    assert request.external_id is None
    assert request.metadata == {}


def test_create_request_rejects_blank_role_and_content() -> None:
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(role="", content="hi")
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(role="user", content="")


def test_create_request_accepts_content_at_max_length_and_rejects_over() -> None:
    at_max = "a" * SESSION_ENTRY_CONTENT_MAX_LENGTH
    assert SessionEntryCreateRequest(role="user", content=at_max).content == at_max
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(role="user", content="a" * (SESSION_ENTRY_CONTENT_MAX_LENGTH + 1))


def test_create_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SessionEntryCreateRequest(role="user", content="hi", entry_id=str(uuid4()))  # type: ignore[call-arg]


def test_create_request_role_is_not_restricted_to_a_closed_set() -> None:
    # role is open-ended TEXT: any label is accepted, including ones that
    # look like provider-privileged roles -- they carry no special meaning.
    for role in ("user", "assistant", "system", "tool", "agent", "workflow", "anything"):
        assert SessionEntryCreateRequest(role=role, content="hi").role == role


# ---------------------------------------------------------------------------
# Service (fake in-memory Unit of Work)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.entries: list[SessionEntry] = []
        self.queries: list[Query] = []
        self.commits = 0


class FakeSessionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, session: Session) -> Session:
        self._store.sessions.append(session)
        return session

    async def get_by_id(self, session_id: UUID) -> Session | None:
        return next((s for s in self._store.sessions if s.id == session_id), None)

    async def get_by_id_for_update(self, session_id: UUID) -> Session | None:
        return await self.get_by_id(session_id)

    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: SessionStatus | None = None,
        key: str | None = None,
    ) -> tuple[list[Session], int]:
        del limit, offset, status, key
        return [], 0


class FakeSessionEntryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, entry: SessionEntry) -> SessionEntry:
        self._store.entries.append(entry)
        return entry

    async def get_by_external_id(self, session_id: UUID, external_id: str) -> SessionEntry | None:
        return next(
            (
                e
                for e in self._store.entries
                if e.session_id == session_id and e.external_id == external_id
            ),
            None,
        )

    async def list_by_session(
        self,
        session_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> list[SessionEntry]:
        items = [e for e in self._store.entries if e.session_id == session_id]
        items.sort(key=lambda e: (e.created_at, e.id), reverse=not ascending)
        return items[offset : offset + limit]

    async def count_by_session(self, session_id: UUID) -> int:
        return len([e for e in self._store.entries if e.session_id == session_id])


class FakeQueryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_by_session(
        self,
        session_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Query]:
        items = [q for q in self._store.queries if q.session_id == session_id]
        items.sort(key=lambda q: (q.created_at, q.id))
        return items[offset : offset + limit]

    async def count_by_session(self, session_id: UUID) -> int:
        return len([q for q in self._store.queries if q.session_id == session_id])


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.sessions = FakeSessionRepository(store)
        self.session_entries = FakeSessionEntryRepository(store)
        self.queries = FakeQueryRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> SessionEntryService:
    def create_uow() -> SessionEntryUnitOfWork:
        return cast(SessionEntryUnitOfWork, FakeUnitOfWork(store))

    return SessionEntryService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def make_session(
    *,
    session_id: UUID | None = None,
    status: SessionStatus = SessionStatus.ACTIVE,
    updated_at: datetime = CREATED_AT,
) -> Session:
    return Session(
        id=session_id or uuid4(),
        key=f"key-{uuid4().hex}",
        name=None,
        status=status,
        metadata_={},
        created_at=CREATED_AT,
        updated_at=updated_at,
        archived_at=None,
    )


def make_query(*, session_id: UUID, created_at: datetime = CREATED_AT) -> Query:
    return Query(
        id=uuid4(),
        query_text=None,
        dataset_ids=[],
        mode="chunks",
        answer=None,
        references={},
        timings={},
        model=None,
        session_id=session_id,
        session_context_entry_ids=[],
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_append_without_external_id_always_creates_new_row() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)

    first = await service.append_entry(
        session.id, SessionEntryCreateRequest(role="user", content="hi")
    )
    second = await service.append_entry(
        session.id, SessionEntryCreateRequest(role="user", content="hi")
    )

    assert first.entry_id != second.entry_id
    assert len(store.entries) == 2


@pytest.mark.asyncio
async def test_append_with_new_external_id_creates_normally() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)

    result = await service.append_entry(
        session.id,
        SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi"),
    )

    assert result.external_id == "ext-1"
    assert result.session_uuid == session.id
    assert len(store.entries) == 1
    assert store.commits == 1


@pytest.mark.asyncio
async def test_replay_same_external_id_same_payload_returns_same_entry() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    request = SessionEntryCreateRequest(
        external_id="ext-1", role="user", content="hi", metadata={"a": 1}
    )

    first = await service.append_entry(session.id, request)
    second = await service.append_entry(session.id, request)

    assert first.entry_id == second.entry_id
    assert len(store.entries) == 1


@pytest.mark.asyncio
async def test_replay_same_external_id_different_role_is_conflict() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    await service.append_entry(
        session.id, SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")
    )

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(
            session.id,
            SessionEntryCreateRequest(external_id="ext-1", role="assistant", content="hi"),
        )

    assert excinfo.value.status_code == 409
    assert len(store.entries) == 1
    assert store.entries[0].role == "user"


@pytest.mark.asyncio
async def test_replay_same_external_id_different_content_is_conflict() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    await service.append_entry(
        session.id, SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")
    )

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(
            session.id,
            SessionEntryCreateRequest(external_id="ext-1", role="user", content="bye"),
        )

    assert excinfo.value.status_code == 409
    assert len(store.entries) == 1
    assert store.entries[0].content == "hi"


@pytest.mark.asyncio
async def test_replay_same_external_id_different_metadata_is_conflict() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    await service.append_entry(
        session.id,
        SessionEntryCreateRequest(
            external_id="ext-1", role="user", content="hi", metadata={"a": 1}
        ),
    )

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(
            session.id,
            SessionEntryCreateRequest(
                external_id="ext-1", role="user", content="hi", metadata={"a": 2}
            ),
        )

    assert excinfo.value.status_code == 409
    assert len(store.entries) == 1
    assert store.entries[0].metadata_ == {"a": 1}


@pytest.mark.asyncio
async def test_append_against_missing_session_is_404() -> None:
    store = FakeStore()
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(uuid4(), SessionEntryCreateRequest(role="user", content="hi"))

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_append_without_external_id_against_archived_session_is_rejected() -> None:
    store = FakeStore()
    session = make_session(status=SessionStatus.ARCHIVED)
    store.sessions.append(session)
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(session.id, SessionEntryCreateRequest(role="user", content="hi"))

    assert excinfo.value.code.value == "SESSION_ARCHIVED"
    assert excinfo.value.status_code == 409
    assert len(store.entries) == 0


@pytest.mark.asyncio
async def test_append_with_unknown_external_id_against_archived_session_is_rejected() -> None:
    store = FakeStore()
    session = make_session(status=SessionStatus.ARCHIVED)
    store.sessions.append(session)
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(
            session.id,
            SessionEntryCreateRequest(external_id="new-id", role="user", content="hi"),
        )

    assert excinfo.value.code.value == "SESSION_ARCHIVED"
    assert len(store.entries) == 0


@pytest.mark.asyncio
async def test_replay_same_payload_succeeds_even_after_archive() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    request = SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")
    admitted = await service.append_entry(session.id, request)

    session.status = SessionStatus.ARCHIVED

    replayed = await service.append_entry(session.id, request)

    assert replayed.entry_id == admitted.entry_id
    assert len(store.entries) == 1


@pytest.mark.asyncio
async def test_conflicting_payload_after_archive_is_idempotency_conflict_not_archived() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    service = service_for(store)
    await service.append_entry(
        session.id, SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")
    )

    session.status = SessionStatus.ARCHIVED

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.append_entry(
            session.id,
            SessionEntryCreateRequest(external_id="ext-1", role="user", content="different"),
        )

    assert excinfo.value.code.value == "IDEMPOTENCY_CONFLICT"
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_append_does_not_update_session_updated_at() -> None:
    store = FakeStore()
    session = make_session(updated_at=CREATED_AT)
    store.sessions.append(session)
    service = service_for(store)

    await service.append_entry(session.id, SessionEntryCreateRequest(role="user", content="hi"))

    assert session.updated_at == CREATED_AT


@pytest.mark.asyncio
async def test_safe_replay_does_not_update_session_updated_at() -> None:
    store = FakeStore()
    session = make_session(updated_at=CREATED_AT)
    store.sessions.append(session)
    service = service_for(store)
    request = SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")

    await service.append_entry(session.id, request)
    assert session.updated_at == CREATED_AT

    # The replay itself (second call, resolves the existing entry) must
    # also leave Session.updated_at untouched -- it is not a new admission.
    await service.append_entry(session.id, request)
    assert session.updated_at == CREATED_AT


@pytest.mark.asyncio
async def test_list_entries_orders_asc_and_desc_with_pagination() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    entries = [
        SessionEntry(
            id=uuid4(),
            session_id=session.id,
            external_id=None,
            role="user",
            content=f"turn-{i}",
            metadata_={},
            created_at=datetime(2026, 1, 1, hour=i, tzinfo=UTC),
        )
        for i in range(3)
    ]
    store.entries.extend(entries)
    service = service_for(store)

    ascending = await service.list_entries(session.id, limit=50, offset=0, ascending=True)
    descending = await service.list_entries(session.id, limit=50, offset=0, ascending=False)
    paged = await service.list_entries(session.id, limit=1, offset=1, ascending=True)

    assert [item.entry_id for item in ascending.items] == [e.id for e in entries]
    assert [item.entry_id for item in descending.items] == [e.id for e in reversed(entries)]
    assert ascending.total == 3
    assert [item.entry_id for item in paged.items] == [entries[1].id]


@pytest.mark.asyncio
async def test_list_entries_reads_do_not_update_session_updated_at() -> None:
    store = FakeStore()
    session = make_session(updated_at=CREATED_AT)
    store.sessions.append(session)
    service = service_for(store)

    await service.list_entries(session.id, limit=50, offset=0, ascending=True)

    assert session.updated_at == CREATED_AT


@pytest.mark.asyncio
async def test_list_entries_allowed_for_archived_session() -> None:
    store = FakeStore()
    session = make_session(status=SessionStatus.ARCHIVED)
    store.sessions.append(session)
    service = service_for(store)

    result = await service.list_entries(session.id, limit=50, offset=0, ascending=True)

    assert result.items == []


@pytest.mark.asyncio
async def test_list_entries_missing_session_is_404() -> None:
    store = FakeStore()
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.list_entries(uuid4(), limit=50, offset=0, ascending=True)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_list_queries_returns_summary_with_nulls_and_pagination() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions.append(session)
    queries = [
        make_query(session_id=session.id, created_at=datetime(2026, 1, 1, hour=i, tzinfo=UTC))
        for i in range(2)
    ]
    store.queries.extend(queries)
    service = service_for(store)

    result = await service.list_queries(session.id, limit=50, offset=0)

    assert [item.query_id for item in result.items] == [q.id for q in queries]
    assert result.total == 2
    assert result.items[0].query_text is None
    assert result.items[0].answer is None


@pytest.mark.asyncio
async def test_list_queries_allowed_for_archived_session_and_does_not_touch_updated_at() -> None:
    store = FakeStore()
    session = make_session(status=SessionStatus.ARCHIVED, updated_at=CREATED_AT)
    store.sessions.append(session)
    service = service_for(store)

    result = await service.list_queries(session.id, limit=50, offset=0)

    assert result.items == []
    assert session.updated_at == CREATED_AT


@pytest.mark.asyncio
async def test_list_queries_missing_session_is_404() -> None:
    store = FakeStore()
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.list_queries(uuid4(), limit=50, offset=0)
    assert excinfo.value.status_code == 404


def test_no_role_privilege_translation_helper_exists() -> None:
    # SM-603 SS 20: role is inert contextual metadata. There is no function
    # anywhere in the service/schema layer that maps a role string onto a
    # provider-native privileged role.
    import sofias_memory.schemas.session_entries as schemas_module
    import sofias_memory.services.session_entries as service_module

    for module in (schemas_module, service_module):
        for name in dir(module):
            assert "provider_role" not in name.lower()
            assert "privileged" not in name.lower()


# ---------------------------------------------------------------------------
# Route-level HTTP contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_entry_routes_return_envelope_and_require_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_uuid = uuid4()
    entry_id = uuid4()
    query_id = uuid4()
    recorded_list_calls: list[dict[str, Any]] = []

    def entry_result(external_id: str | None = None) -> SessionEntryResult:
        return SessionEntryResult(
            entry_id=entry_id,
            session_uuid=session_uuid,
            external_id=external_id,
            role="user",
            content="hi",
            metadata={},
            created_at=CREATED_AT,
        )

    class FakeSessionEntryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def append_entry(
            self, session_uuid: UUID, request: SessionEntryCreateRequest
        ) -> SessionEntryResult:
            return entry_result(external_id=request.external_id)

        async def list_entries(
            self, session_uuid: UUID, *, limit: int, offset: int, ascending: bool
        ) -> SessionEntryListResult:
            recorded_list_calls.append({"limit": limit, "offset": offset, "ascending": ascending})
            return SessionEntryListResult(
                items=[entry_result()], limit=limit, offset=offset, total=1
            )

        async def list_queries(
            self, session_uuid: UUID, *, limit: int, offset: int
        ) -> SessionQueryListResult:
            return SessionQueryListResult(
                items=[
                    SessionQuerySummaryResult(
                        query_id=query_id,
                        dataset_ids=[],
                        mode="chunks",
                        query_text=None,
                        answer=None,
                        model=None,
                        created_at=CREATED_AT,
                    )
                ],
                limit=limit,
                offset=offset,
                total=1,
            )

    monkeypatch.setattr(
        "sofias_memory.api.routes.sessions.SessionEntryService", FakeSessionEntryService
    )
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key = await client.post(f"/api/v1/sessions/{session_uuid}/entries")
        created = await client.post(
            f"/api/v1/sessions/{session_uuid}/entries",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"role": "user", "content": "hi", "external_id": "ext-1"},
        )
        listed_desc = await client.get(
            f"/api/v1/sessions/{session_uuid}/entries?order=desc&limit=1&offset=0",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        queries = await client.get(
            f"/api/v1/sessions/{session_uuid}/queries",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert missing_key.status_code == 401
    assert created.status_code == 201
    assert created.json()["data"]["entry_id"] == str(entry_id)
    assert created.json()["data"]["external_id"] == "ext-1"
    assert listed_desc.status_code == 200
    assert recorded_list_calls == [{"limit": 1, "offset": 0, "ascending": False}]
    assert queries.status_code == 200
    assert queries.json()["data"]["items"][0]["query_id"] == str(query_id)
    assert queries.json()["data"]["items"][0]["query_text"] is None
