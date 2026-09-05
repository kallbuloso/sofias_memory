from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres.models import Session
from sofias_memory.schemas.sessions import (
    SessionCreateRequest,
    SessionListResult,
    SessionResult,
    SessionUpdateRequest,
)
from sofias_memory.services.sessions import (
    SessionService,
    SessionUnitOfWork,
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


def test_create_request_normalizes_and_preserves_case() -> None:
    request = SessionCreateRequest(session_id="  sofias-assistant:Conversation:42  ")
    assert request.session_id == "sofias-assistant:Conversation:42"


def test_create_request_rejects_blank_session_id() -> None:
    with pytest.raises(ValidationError):
        SessionCreateRequest(session_id="   ")


def test_create_request_rejects_over_255_char_session_id() -> None:
    with pytest.raises(ValidationError):
        SessionCreateRequest(session_id="a" * 256)


def test_create_request_omitted_session_id_defaults_to_none() -> None:
    request = SessionCreateRequest()
    assert request.session_id is None
    assert request.name is None
    assert request.metadata == {}


def test_create_request_trims_name_and_rejects_blank() -> None:
    assert SessionCreateRequest(name="  Planning  ").name == "Planning"
    with pytest.raises(ValidationError):
        SessionCreateRequest(name="   ")


def test_create_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SessionCreateRequest(status="archived")  # type: ignore[call-arg]


def test_update_request_rejects_empty_patch() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateRequest()


def test_update_request_rejects_null_metadata() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateRequest(metadata=None, name="kept")  # type: ignore[arg-type]


def test_update_request_allows_null_name_to_clear_it() -> None:
    request = SessionUpdateRequest(name=None, metadata={"a": 1})
    assert "name" in request.model_fields_set
    assert request.name is None


def test_update_request_rejects_forbidden_fields() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateRequest(session_id="x", name="y")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SessionUpdateRequest(status="archived", name="y")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Service (fake in-memory Unit of Work)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.commits = 0


class FakeSessionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, session: Session) -> Session:
        if any(existing.key == session.key for existing in self._store.sessions):
            raise IntegrityError("insert", {}, Exception("duplicate key value"))
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
        items = list(self._store.sessions)
        if status is not None:
            items = [s for s in items if s.status == status]
        if key is not None:
            items = [s for s in items if s.key == key]
        ordered = sorted(items, key=lambda s: (s.created_at, s.id))
        return ordered[offset : offset + limit], len(ordered)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.sessions = FakeSessionRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> SessionService:
    def create_uow() -> SessionUnitOfWork:
        return cast(SessionUnitOfWork, FakeUnitOfWork(store))

    return SessionService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def make_session(
    *,
    key: str,
    session_id: UUID | None = None,
    name: str | None = None,
    status: SessionStatus = SessionStatus.ACTIVE,
    created_at: datetime = CREATED_AT,
    metadata: dict[str, Any] | None = None,
) -> Session:
    return Session(
        id=session_id or uuid4(),
        key=key,
        name=name,
        status=status,
        metadata_=metadata or {},
        created_at=created_at,
        updated_at=created_at,
        archived_at=None,
    )


@pytest.mark.asyncio
async def test_create_session_uses_explicit_session_id() -> None:
    store = FakeStore()
    result = await service_for(store).create_session(
        SessionCreateRequest(session_id="sofias-assistant:conversation:1", name="Planning")
    )

    assert result.session_id == "sofias-assistant:conversation:1"
    assert result.name == "Planning"
    assert result.status == SessionStatus.ACTIVE
    assert result.metadata == {}
    assert store.commits == 1


@pytest.mark.asyncio
async def test_create_session_omitted_id_uses_single_generated_uuid() -> None:
    store = FakeStore()
    result = await service_for(store).create_session(SessionCreateRequest())

    assert result.session_id == str(result.session_uuid)
    assert UUID(result.session_id) == result.session_uuid


@pytest.mark.asyncio
async def test_create_session_duplicate_session_id_is_conflict_not_upsert() -> None:
    store = FakeStore()
    service = service_for(store)
    await service.create_session(SessionCreateRequest(session_id="dup", name="First"))

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.create_session(SessionCreateRequest(session_id="dup", name="Second"))

    assert excinfo.value.status_code == 409
    assert len(store.sessions) == 1
    assert store.sessions[0].name == "First"


@pytest.mark.asyncio
async def test_list_sessions_paginates_filters_and_orders_deterministically() -> None:
    store = FakeStore()
    older = make_session(key="Case", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = make_session(key="case", created_at=datetime(2026, 1, 2, tzinfo=UTC))
    archived = make_session(
        key="archived-one",
        status=SessionStatus.ARCHIVED,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    store.sessions.extend([newer, older, archived])
    service = service_for(store)

    all_active = await service.list_sessions(
        limit=50, offset=0, status=SessionStatus.ACTIVE, session_id=None
    )
    paged = await service.list_sessions(limit=1, offset=0, status=None, session_id=None)
    exact_lower = await service.list_sessions(limit=50, offset=0, status=None, session_id="case")
    exact_upper = await service.list_sessions(limit=50, offset=0, status=None, session_id="Case")

    assert [item.session_id for item in all_active.items] == ["Case", "case"]
    assert paged.total == 3
    assert len(paged.items) == 1
    assert [item.session_id for item in exact_lower.items] == ["case"]
    assert [item.session_id for item in exact_upper.items] == ["Case"]


@pytest.mark.asyncio
async def test_list_sessions_rejects_blank_session_id_filter() -> None:
    store = FakeStore()
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service_for(store).list_sessions(limit=50, offset=0, status=None, session_id="   ")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_get_session_existing_and_missing() -> None:
    store = FakeStore()
    session = make_session(key="a")
    store.sessions.append(session)
    service = service_for(store)

    found = await service.get_session(session.id)
    assert found.session_uuid == session.id

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.get_session(uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_update_session_name_metadata_and_clearing_name() -> None:
    store = FakeStore()
    session = make_session(key="a", name="Original", metadata={"x": 1})
    store.sessions.append(session)
    service = service_for(store)

    renamed = await service.update_session(session.id, SessionUpdateRequest(name="New name"))
    assert renamed.name == "New name"
    assert renamed.metadata == {"x": 1}
    assert renamed.updated_at > CREATED_AT

    replaced = await service.update_session(session.id, SessionUpdateRequest(metadata={"y": 2}))
    assert replaced.metadata == {"y": 2}
    assert replaced.name == "New name"

    cleared = await service.update_session(session.id, SessionUpdateRequest(name=None))
    assert cleared.name is None
    assert cleared.metadata == {"y": 2}


@pytest.mark.asyncio
async def test_update_session_works_while_archived() -> None:
    store = FakeStore()
    session = make_session(key="a", status=SessionStatus.ARCHIVED)
    store.sessions.append(session)
    service = service_for(store)

    result = await service.update_session(session.id, SessionUpdateRequest(name="Still works"))

    assert result.name == "Still works"
    assert result.status == SessionStatus.ARCHIVED


@pytest.mark.asyncio
async def test_archive_and_restore_lifecycle_and_idempotency() -> None:
    store = FakeStore()
    session = make_session(key="a")
    store.sessions.append(session)
    service = service_for(store)

    archived = await service.archive_session(session.id)
    assert archived.status == SessionStatus.ARCHIVED
    assert archived.archived_at is not None
    archived_timestamp = archived.archived_at
    updated_timestamp = archived.updated_at

    archived_again = await service.archive_session(session.id)
    assert archived_again.status == SessionStatus.ARCHIVED
    assert archived_again.archived_at == archived_timestamp
    assert archived_again.updated_at == updated_timestamp

    restored = await service.restore_session(session.id)
    assert restored.status == SessionStatus.ACTIVE
    assert restored.archived_at is None
    restored_updated_timestamp = restored.updated_at

    restored_again = await service.restore_session(session.id)
    assert restored_again.status == SessionStatus.ACTIVE
    assert restored_again.updated_at == restored_updated_timestamp


# ---------------------------------------------------------------------------
# Route-level HTTP contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_routes_return_envelope_and_require_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_uuid = uuid4()

    def result(
        name: str | None = "Docs",
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> SessionResult:
        return SessionResult(
            session_uuid=session_uuid,
            session_id="ext-key",
            name=name,
            status=status,
            metadata={},
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
            archived_at=None,
        )

    class FakeSessionService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def create_session(self, request: SessionCreateRequest) -> SessionResult:
            return result(name=request.name)

        async def list_sessions(
            self, *, limit: int, offset: int, status: object, session_id: object
        ) -> SessionListResult:
            assert (limit, offset) == (2, 1)
            return SessionListResult(items=[result()], limit=limit, offset=offset, total=3)

        async def get_session(self, session_uuid: UUID) -> SessionResult:
            return result()

        async def update_session(
            self, session_uuid: UUID, request: SessionUpdateRequest
        ) -> SessionResult:
            return result(name=request.name)

        async def archive_session(self, session_uuid: UUID) -> SessionResult:
            return result(status=SessionStatus.ARCHIVED)

        async def restore_session(self, session_uuid: UUID) -> SessionResult:
            return result(status=SessionStatus.ACTIVE)

    monkeypatch.setattr("sofias_memory.api.routes.sessions.SessionService", FakeSessionService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key = await client.get("/api/v1/sessions")
        created = await client.post(
            "/api/v1/sessions",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"name": "Docs"},
        )
        listed = await client.get(
            "/api/v1/sessions?limit=2&offset=1",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        fetched = await client.get(
            f"/api/v1/sessions/{session_uuid}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        invalid_uuid = await client.get(
            "/api/v1/sessions/not-a-uuid",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        patched = await client.patch(
            f"/api/v1/sessions/{session_uuid}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"name": "Renamed"},
        )
        archived = await client.post(
            f"/api/v1/sessions/{session_uuid}/archive",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        restored = await client.post(
            f"/api/v1/sessions/{session_uuid}/restore",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert missing_key.status_code == 401
    assert created.status_code == 201
    assert created.json()["data"]["session_uuid"] == str(session_uuid)
    assert listed.json()["data"]["limit"] == 2
    assert listed.json()["data"]["offset"] == 1
    assert listed.json()["data"]["total"] == 3
    assert fetched.status_code == 200
    assert invalid_uuid.status_code == 422
    assert patched.json()["data"]["name"] == "Renamed"
    assert archived.json()["data"]["status"] == "archived"
    assert restored.json()["data"]["status"] == "active"
