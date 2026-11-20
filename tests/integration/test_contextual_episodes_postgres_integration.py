"""Real-PostgreSQL integration suite for SM-CTX-001 First-Class Episodes
(ADR-0017, Contract v1 Contextual Project Memory, handoff 09).

Only the embedding provider is faked; PostgreSQL+pgvector, the real
``EpisodeService`` and the real HTTP app are used. Neo4j is never needed
(Contextual Project Memory is PostgreSQL-only).

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXTUAL_EPISODES_POSTGRES_TESTS=1`` plus
``DATABASE_URL`` pointing at a dedicated, discardable database already
migrated to head (0019). Every test uses its own random Project Scope and
removes the rows it created.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.domain import protect_contextual_idempotency_key
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    ContextualMemoryIdempotency,
    Episode,
    EpisodeProvenance,
    GraphOutbox,
    MemoryItem,
    MemoryProvenance,
    PipelineRun,
    PipelineStep,
    Session,
    SessionEntry,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.observability.logging import clear_log_context, configure_logging
from sofias_memory.schemas.episodes import EpisodeCreateRequest, EpisodeInvalidateRequest
from sofias_memory.services.episodes import EpisodeService
from tests.unit._app_factory import create_app

CONTEXTUAL_EPISODES_POSTGRES_ENV = "SOFIAS_MEMORY_RUN_CONTEXTUAL_EPISODES_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
EMBEDDING_DIMENSIONS = 3072
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


# --- fakes --------------------------------------------------------------------


def axis_vector(cosine: float) -> list[float]:
    """Unit vector whose cosine with the query axis (e0) is ``cosine``."""

    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[0] = cosine
    vector[1] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return vector


class FakeEmbeddingClient:
    """Deterministic: maps the canonical text's summary (or a raw query) to a
    controllable vector; anything unknown gets a constant vector."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for value in texts:
            first_line = value.split("\n", 1)[0]
            key = first_line.removeprefix("Summary: ")
            out.append(self.vectors.get(key, axis_vector(0.25)))
        return out


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


class BarrierEmbeddingClient:
    def __init__(self, participants: int) -> None:
        self._barrier = asyncio.Barrier(participants)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        await self._barrier.wait()
        return [axis_vector(0.5) for _ in texts]


class BlockingEmbeddingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [axis_vector(0.5) for _ in texts]


# --- fixtures/helpers -----------------------------------------------------------


def _contextual_episodes_test_database_url(env: dict[str, str]) -> str:
    if env.get(CONTEXTUAL_EPISODES_POSTGRES_ENV) != "1":
        pytest.skip(f"set {CONTEXTUAL_EPISODES_POSTGRES_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


def test_contextual_episodes_suite_skips_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _contextual_episodes_test_database_url({})


@pytest_asyncio.fixture()
async def session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _contextual_episodes_test_database_url(dict(os.environ))
    engine = create_async_engine_from_settings(load_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password="unused-neo4j-password",
        llm_api_key="sk-fake-test-key",
        cognitive_idempotency_hmac_key=HMAC_KEY,
        app_env="test",
    )


def service(factory: AsyncSessionFactory, client: object) -> EpisodeService:
    return EpisodeService(
        make_settings(),
        embedding_client=client,  # type: ignore[arg-type]
        session_factory=factory,
    )


def build_app(
    factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    client: object | None = None,
) -> Any:
    embedding_client = client or FakeEmbeddingClient()
    monkeypatch.setattr(
        "sofias_memory.api.routes.episodes.OpenAIEmbeddingClient",
        lambda settings: embedding_client,
    )
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=factory,
    )


def http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def new_scope() -> str:
    return f"project:ctx-test-{uuid4().hex[:12]}"


def key(prefix: str = "k") -> str:
    return f"{prefix}-{uuid4().hex}"


def payload(scope: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scope": scope,
        "context_key": "feature:v1",
        "kind": "decision",
        "summary": f"Episode foi escolhido como agregado first-class {uuid4().hex}.",
        "situation": "Foram avaliadas alternativas de modelagem.",
        "reason": "Historical experience possui semântica diferente de current truth.",
        "outcome": "MemoryItem continua profile|semantic.",
        "occurred_at": "2026-11-18T20:00:00Z",
        "provenance": {
            "origin_kind": "user_asserted",
            "source_system": "manual-validation",
            "conversation_ref": "sm-ctx-001",
            "turn_ref": "create",
        },
    }
    body.update(overrides)
    return body


def request(scope: str, **overrides: Any) -> EpisodeCreateRequest:
    return EpisodeCreateRequest.model_validate(payload(scope, **overrides))


async def cleanup(factory: AsyncSessionFactory, scopes: Sequence[str], ids: set[UUID]) -> None:
    async with factory() as session:
        found = await session.scalars(select(Episode.id).where(Episode.scope.in_(list(scopes))))
        all_ids = set(found.all()) | ids
        if all_ids:
            await session.execute(
                delete(ContextualMemoryIdempotency).where(
                    ContextualMemoryIdempotency.result_resource_id.in_(all_ids)
                    | ContextualMemoryIdempotency.target_resource_id.in_(all_ids)
                )
            )
            await session.execute(
                update(Episode)
                .where(Episode.replacement_episode_id.in_(all_ids))
                .values(replacement_episode_id=None)
            )
            await session.execute(delete(Episode).where(Episode.id.in_(all_ids)))
        await session.commit()


async def count(factory: AsyncSessionFactory, model: type, *where: Any) -> int:
    async with factory() as session:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(await session.scalar(statement) or 0)


ISOLATION_MODELS: tuple[type, ...] = (
    GraphOutbox,
    PipelineRun,
    PipelineStep,
    MemoryItem,
    MemoryProvenance,
    CognitiveMemoryIdempotency,
    Session,
    SessionEntry,
)


async def isolation_counts(factory: AsyncSessionFactory) -> dict[str, int]:
    return {model.__name__: await count(factory, model) for model in ISOLATION_MODELS}


# --- create / get / restart ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_create_get_returns_active_episode_with_server_uuid(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    app = build_app(session_factory, monkeypatch)
    try:
        async with http(app) as client:
            body = payload(scope)
            created = await client.post(
                "/api/v1/episodes", json=body, headers={**HEADERS, "Idempotency-Key": key()}
            )
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            assert data["lifecycle"] == "active"
            assert "embedding" not in data
            episode_id = UUID(data["episode_id"])
            assert data["scope"] == scope
            assert data["context_key"] == "feature:v1"
            assert data["kind"] == "decision"
            assert data["summary"] == body["summary"]
            assert data["provenance"] == body["provenance"]
            assert data["occurred_at"].startswith("2026-11-18T20:00:00")
            assert data["created_at"] != data["occurred_at"]
            assert data["invalidated_at"] is None and data["forgotten_at"] is None

            got = await client.get(f"/api/v1/episodes/{episode_id}", headers=HEADERS)
            assert got.status_code == 200
            assert got.json()["data"] == data

            client_id = await client.post(
                "/api/v1/episodes",
                json={**payload(scope), "episode_id": str(uuid4())},
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert client_id.status_code == 422

            missing = await client.get(f"/api/v1/episodes/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "EPISODE_NOT_FOUND"

        async with session_factory() as session:
            row = await session.get(Episode, episode_id)
            assert row is not None and row.embedding is not None
            assert len(row.embedding) == EMBEDDING_DIMENSIONS
            assert row.occurred_at == datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
            assert row.created_at > row.occurred_at
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_wide_episode_keeps_null_context_key_never_main(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        wide = await svc.create(request(scope, context_key=None), idempotency_key=key())
        main = await svc.create(request(scope, context_key="main"), idempotency_key=key())
        assert wide.context_key is None
        async with session_factory() as session:
            stored = await session.scalar(
                select(Episode.context_key).where(Episode.id == wide.episode_id)
            )
        assert stored is None

        main_only = await svc.list_episodes(
            scope=scope, context_key="main", kind=None, lifecycle=None, limit=50, cursor=None
        )
        assert [item.episode_id for item in main_only.items] == [main.episode_id]
        everything = await svc.list_episodes(
            scope=scope, context_key=None, kind=None, lifecycle=None, limit=50, cursor=None
        )
        assert {item.episode_id for item in everything.items} == {wide.episode_id, main.episode_id}
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_survives_restart_with_new_engine_app_and_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contextual_episodes_test_database_url(dict(os.environ))
    scope = new_scope()
    engine_one = create_async_engine_from_settings(load_settings())
    factory_one = create_session_factory(engine_one)
    try:
        async with http(build_app(factory_one, monkeypatch)) as client:
            created = await client.post(
                "/api/v1/episodes",
                json=payload(scope),
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert created.status_code == 201
            original = created.json()["data"]
    finally:
        await dispose_async_engine(engine_one)

    engine_two = create_async_engine_from_settings(load_settings())
    factory_two = create_session_factory(engine_two)
    try:
        async with http(build_app(factory_two, monkeypatch)) as client:
            got = await client.get(f"/api/v1/episodes/{original['episode_id']}", headers=HEADERS)
            assert got.status_code == 200
            assert got.json()["data"] == original

        child = (
            "import asyncio, uuid\n"
            "from sofias_memory.config import load_settings\n"
            "from sofias_memory.infrastructure.postgres import create_async_engine_from_settings,"
            " create_session_factory, dispose_async_engine\n"
            "from sofias_memory.services.episodes import EpisodeService\n"
            "async def main():\n"
            "    settings = load_settings()\n"
            "    engine = create_async_engine_from_settings(settings)\n"
            "    try:\n"
            "        svc = EpisodeService(settings, embedding_client=None,"
            " session_factory=create_session_factory(engine))\n"
            f"        result = await svc.get(uuid.UUID('{original['episode_id']}'))\n"
            "        print(result.model_dump_json())\n"
            "    finally:\n"
            "        await dispose_async_engine(engine)\n"
            "asyncio.run(main())\n"
        )
        completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-authored script
            [sys.executable, "-c", child],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(os.environ),
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        from_new_process = json.loads(completed.stdout.strip().splitlines()[-1])
        for field in ("episode_id", "scope", "context_key", "kind", "summary", "lifecycle"):
            assert from_new_process[field] == original[field]
        assert from_new_process["provenance"] == original["provenance"]
    finally:
        await cleanup(factory_two, [scope], set())
        await dispose_async_engine(engine_two)


# --- idempotency --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_same_request_replays_without_duplicate_or_re_embedding(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    fake = FakeEmbeddingClient()
    svc = service(session_factory, fake)
    idem = key("replay")
    try:
        first = await svc.create(request(scope, summary="replay summary"), idempotency_key=idem)
        calls = len(fake.calls)
        second = await svc.create(request(scope, summary="replay summary"), idempotency_key=idem)
        assert second == first
        assert len(fake.calls) == calls
        assert await count(session_factory, Episode, Episode.scope == scope) == 1
        protected = protect_contextual_idempotency_key(
            secret=HMAC_KEY.encode(), idempotency_key=idem
        )
        async with session_factory() as session:
            ledger = (
                await session.scalars(
                    select(ContextualMemoryIdempotency).where(
                        ContextualMemoryIdempotency.result_resource_id == first.episode_id
                    )
                )
            ).all()
        assert len(ledger) == 1
        entry = ledger[0]
        assert entry.idempotency_key == protected
        assert entry.operation.value == "episode_create"
        assert entry.target_resource_id is None
        stored = json.dumps(
            [entry.idempotency_key, entry.request_digest, str(entry.result_resource_id)]
        )
        for raw in (idem, scope, "replay summary", "manual-validation", "sm-ctx-001"):
            assert raw not in stored
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_different_request_conflicts_and_cross_operation_reuse_conflicts(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    app = build_app(session_factory, monkeypatch)
    idem = key("conflict")
    try:
        async with http(app) as client:
            headers = {**HEADERS, "Idempotency-Key": idem}
            first = await client.post("/api/v1/episodes", json=payload(scope), headers=headers)
            assert first.status_code == 201
            episode_id = first.json()["data"]["episode_id"]
            different = await client.post(
                "/api/v1/episodes", json=payload(scope, summary="different"), headers=headers
            )
            assert different.status_code == 409
            assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            assert await count(session_factory, Episode, Episode.scope == scope) == 1

            reused = await client.post(f"/api/v1/episodes/{episode_id}/forget", headers=headers)
            assert reused.status_code == 409
            assert reused.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            still = await client.get(f"/api/v1/episodes/{episode_id}", headers=HEADERS)
            assert still.json()["data"]["lifecycle"] == "active"

            reserved = await client.post(
                "/api/v1/episodes",
                json=payload(scope),
                headers={**HEADERS, "Idempotency-Key": "sys:internal"},
            )
            assert reserved.status_code == 400
            assert reserved.json()["error"]["code"] == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_same_request_converges_to_one_episode(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    barrier = BarrierEmbeddingClient(participants=2)
    idem = key("race")
    body = request(scope)
    try:
        results = await asyncio.gather(
            service(session_factory, barrier).create(body, idempotency_key=idem),
            service(session_factory, barrier).create(body, idempotency_key=idem),
        )
        assert results[0].episode_id == results[1].episode_id
        assert await count(session_factory, Episode, Episode.scope == scope) == 1
        assert (
            await count(
                session_factory,
                EpisodeProvenance,
                EpisodeProvenance.episode_id == results[0].episode_id,
            )
            == 1
        )
        protected = protect_contextual_idempotency_key(
            secret=HMAC_KEY.encode(), idempotency_key=idem
        )
        assert (
            await count(
                session_factory,
                ContextualMemoryIdempotency,
                ContextualMemoryIdempotency.idempotency_key == protected,
            )
            == 1
        )
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_different_request_one_wins_one_conflicts(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    barrier = BarrierEmbeddingClient(participants=2)
    idem = key("race-conflict")
    try:
        results = await asyncio.gather(
            service(session_factory, barrier).create(
                request(scope, summary="race A"), idempotency_key=idem
            ),
            service(session_factory, barrier).create(
                request(scope, summary="race B"), idempotency_key=idem
            ),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        successes = [r for r in results if not isinstance(r, BaseException)]
        assert len(successes) == 1 and len(failures) == 1
        assert isinstance(failures[0], SofiasMemoryError)
        assert failures[0].code.value == "IDEMPOTENCY_CONFLICT"
        assert await count(session_factory, Episode, Episode.scope == scope) == 1
        assert await count(session_factory, EpisodeProvenance) >= 1
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_lifecycle_mutations_are_serialized(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        target = await svc.create(request(scope), idempotency_key=key())
        idem = key("inv-race")
        invalidate = EpisodeInvalidateRequest(reason="concurrent")
        same = await asyncio.gather(
            svc.invalidate(target.episode_id, invalidate, idempotency_key=idem),
            svc.invalidate(target.episode_id, invalidate, idempotency_key=idem),
        )
        assert same[0] == same[1]
        assert same[0].lifecycle.value == "invalidated"
        assert (
            await count(
                session_factory,
                ContextualMemoryIdempotency,
                ContextualMemoryIdempotency.operation == "episode_invalidate",
                ContextualMemoryIdempotency.target_resource_id == target.episode_id,
            )
            == 1
        )

        other = await svc.create(request(scope), idempotency_key=key())
        mixed = await asyncio.gather(
            svc.invalidate(
                other.episode_id, EpisodeInvalidateRequest(reason="x"), idempotency_key=key()
            ),
            svc.forget(other.episode_id, idempotency_key=key()),
            svc.forget(other.episode_id, idempotency_key=key()),
            return_exceptions=True,
        )
        invalidate_outcome, *forget_outcomes = mixed
        for outcome in forget_outcomes:
            assert not isinstance(outcome, BaseException)
            assert outcome.lifecycle.value == "forgotten"
        if isinstance(invalidate_outcome, BaseException):
            assert isinstance(invalidate_outcome, SofiasMemoryError)
            assert invalidate_outcome.code.value == "EPISODE_STATE_CONFLICT"
        final = await svc.get(other.episode_id)
        assert final.lifecycle.value == "forgotten"
        assert final.summary is None
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_is_resolved_before_lifecycle_rejection(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        created = await svc.create(request(scope), idempotency_key=key())
        invalidate_key = key("inv")
        invalidate = EpisodeInvalidateRequest(reason="found inaccurate")
        first = await svc.invalidate(created.episode_id, invalidate, idempotency_key=invalidate_key)
        assert first.lifecycle.value == "invalidated"

        forget_key = key("forget")
        forgotten = await svc.forget(created.episode_id, idempotency_key=forget_key)
        assert forgotten.lifecycle.value == "forgotten"

        # Response-lost retry of the ORIGINAL invalidate after a later forget:
        # replay wins over the FORGOTTEN -> conflict rule (Contract SS 36.3).
        replayed = await svc.invalidate(
            created.episode_id, invalidate, idempotency_key=invalidate_key
        )
        assert replayed.episode_id == created.episode_id
        assert replayed.lifecycle.value == "forgotten"

        # A genuinely new invalidate is a state conflict.
        with pytest.raises(SofiasMemoryError) as exc_info:
            await svc.invalidate(created.episode_id, invalidate, idempotency_key=key())
        assert exc_info.value.code.value == "EPISODE_STATE_CONFLICT"

        # Forget replay and new-key Forget are both 200 no-ops.
        again = await svc.forget(created.episode_id, idempotency_key=forget_key)
        fresh = await svc.forget(created.episode_id, idempotency_key=key())
        assert again.forgotten_at == fresh.forgotten_at == forgotten.forgotten_at

        # Same key, different target -> conflict.
        another = await svc.create(request(scope), idempotency_key=key())
        with pytest.raises(SofiasMemoryError) as exc_info:
            await svc.forget(another.episode_id, idempotency_key=forget_key)
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        await cleanup(session_factory, [scope], set())


# --- lifecycle over HTTP + tombstone --------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_invalidate_then_forget_produces_scrubbed_tombstone(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    app = build_app(session_factory, monkeypatch)
    before = await isolation_counts(session_factory)
    try:
        async with http(app) as client:
            body = payload(scope, confidence=0.75)
            created = await client.post(
                "/api/v1/episodes", json=body, headers={**HEADERS, "Idempotency-Key": key()}
            )
            episode_id = created.json()["data"]["episode_id"]

            listed = await client.get("/api/v1/episodes", params={"scope": scope}, headers=HEADERS)
            assert [i["episode_id"] for i in listed.json()["data"]["items"]] == [episode_id]

            invalidated = await client.post(
                f"/api/v1/episodes/{episode_id}/invalidate",
                json={"reason": "The original episode was found to be inaccurate."},
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert invalidated.status_code == 200
            inv = invalidated.json()["data"]
            assert inv["lifecycle"] == "invalidated"
            assert inv["summary"] == body["summary"]
            assert inv["invalidated_at"] is not None

            again = await client.post(
                f"/api/v1/episodes/{episode_id}/invalidate",
                json={"reason": "another reason"},
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert again.status_code == 200
            assert again.json()["data"]["invalidated_at"] == inv["invalidated_at"]

            default_list = await client.get(
                "/api/v1/episodes", params={"scope": scope}, headers=HEADERS
            )
            assert default_list.json()["data"]["items"] == []
            invalidated_list = await client.get(
                "/api/v1/episodes",
                params={"scope": scope, "lifecycle": "invalidated"},
                headers=HEADERS,
            )
            assert [i["episode_id"] for i in invalidated_list.json()["data"]["items"]] == [
                episode_id
            ]

            forgotten = await client.post(
                f"/api/v1/episodes/{episode_id}/forget",
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert forgotten.status_code == 200
            tomb = forgotten.json()["data"]
            assert tomb["lifecycle"] == "forgotten"
            for field in (
                "scope",
                "context_key",
                "kind",
                "summary",
                "situation",
                "reason",
                "outcome",
                "occurred_at",
                "confidence",
            ):
                assert tomb[field] is None, field
            assert tomb["provenance"] == {
                "origin_kind": "user_asserted",
                "source_system": "manual-validation",
                "conversation_ref": None,
                "turn_ref": None,
            }
            assert tomb["forgotten_at"] is not None
            assert tomb["invalidated_at"] == inv["invalidated_at"]
            assert tomb["created_at"] == created.json()["data"]["created_at"]

            got = await client.get(f"/api/v1/episodes/{episode_id}", headers=HEADERS)
            assert got.json()["data"] == tomb

            for lifecycle in ("active", "invalidated", "forgotten"):
                scoped = await client.get(
                    "/api/v1/episodes",
                    params={"scope": scope, "lifecycle": lifecycle},
                    headers=HEADERS,
                )
                assert scoped.json()["data"]["items"] == []

            conflict = await client.post(
                f"/api/v1/episodes/{episode_id}/invalidate",
                json={"reason": "late"},
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "EPISODE_STATE_CONFLICT"

        async with session_factory() as session:
            row = await session.get(Episode, UUID(episode_id))
            prov = await session.get(EpisodeProvenance, UUID(episode_id))
            assert row is not None and prov is not None
            for column in (
                "scope",
                "context_key",
                "kind",
                "summary",
                "situation",
                "reason",
                "outcome",
                "occurred_at",
                "embedding",
                "confidence",
                "invalidation_reason",
                "replacement_episode_id",
            ):
                assert getattr(row, column) is None, column
            assert row.invalidated_at is not None
            assert prov.conversation_ref is None and prov.turn_ref is None
            raw_row = (
                await session.execute(
                    text("SELECT row_to_json(e)::text FROM episodes e WHERE id = :id"),
                    {"id": UUID(episode_id)},
                )
            ).scalar_one()
            for leaked in (body["summary"], body["situation"], scope, "inaccurate", "sm-ctx-001"):
                assert leaked not in raw_row
        assert await isolation_counts(session_factory) == before
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_replacement_validation(
    session_factory: AsyncSessionFactory,
) -> None:
    scope, other_scope = new_scope(), new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        target = await svc.create(request(scope), idempotency_key=key())
        replacement = await svc.create(request(scope), idempotency_key=key())
        foreign = await svc.create(request(other_scope), idempotency_key=key())
        gone = await svc.create(request(scope), idempotency_key=key())
        await svc.forget(gone.episode_id, idempotency_key=key())

        for candidate in (uuid4(), gone.episode_id, foreign.episode_id, target.episode_id):
            with pytest.raises(SofiasMemoryError) as exc_info:
                await svc.invalidate(
                    target.episode_id,
                    EpisodeInvalidateRequest(reason="r", replacement_episode_id=candidate),
                    idempotency_key=key(),
                )
            assert exc_info.value.code.value == "INVALID_REQUEST"
            assert exc_info.value.status_code == 422
        assert (await svc.get(target.episode_id)).lifecycle.value == "active"

        with pytest.raises(SofiasMemoryError) as missing:
            await svc.invalidate(
                uuid4(), EpisodeInvalidateRequest(reason="r"), idempotency_key=key()
            )
        assert missing.value.code.value == "EPISODE_NOT_FOUND"

        ok = await svc.invalidate(
            target.episode_id,
            EpisodeInvalidateRequest(reason="r", replacement_episode_id=replacement.episode_id),
            idempotency_key=key(),
        )
        assert ok.lifecycle.value == "invalidated"
        async with session_factory() as session:
            row = await session.get(Episode, target.episode_id)
            assert row is not None
            assert row.replacement_episode_id == replacement.episode_id
            assert row.invalidation_reason == "r"
        assert (await svc.get(replacement.episode_id)).lifecycle.value == "active"
    finally:
        await cleanup(session_factory, [scope, other_scope], set())


# --- listing --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_listing_order_filters_pagination_and_project_isolation(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope, other_scope = new_scope(), new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    try:
        specs = [
            (base, "feature:a", "decision"),
            (base, "feature:a", "incident"),
            (base + timedelta(days=1), "feature:b", "decision"),
            (base - timedelta(days=1), None, "learning"),
            (base + timedelta(days=2), "feature:a", "milestone"),
        ]
        created = []
        for occurred_at, context_key, kind in specs:
            created.append(
                await svc.create(
                    request(
                        scope,
                        occurred_at=occurred_at.isoformat(),
                        context_key=context_key,
                        kind=kind,
                    ),
                    idempotency_key=key(),
                )
            )
        await svc.create(request(other_scope), idempotency_key=key())

        expected = sorted(
            created,
            key=lambda e: (-cast(datetime, e.occurred_at).timestamp(), str(e.episode_id)),
        )
        all_items = await svc.list_episodes(
            scope=scope, context_key=None, kind=None, lifecycle=None, limit=100, cursor=None
        )
        assert [i.episode_id for i in all_items.items] == [e.episode_id for e in expected]
        assert all_items.next_cursor is None

        app = build_app(session_factory, monkeypatch)
        paged: list[str] = []
        cursor: str | None = None
        async with http(app) as client:
            while True:
                params: dict[str, Any] = {"scope": scope, "limit": 2}
                if cursor:
                    params["cursor"] = cursor
                page = await client.get("/api/v1/episodes", params=params, headers=HEADERS)
                assert page.status_code == 200
                data = page.json()["data"]
                assert len(data["items"]) <= 2
                paged.extend(item["episode_id"] for item in data["items"])
                cursor = data["next_cursor"]
                if cursor is None:
                    break
            assert paged == [str(e.episode_id) for e in expected]

            bad = await client.get(
                "/api/v1/episodes", params={"scope": scope, "cursor": "garbage!"}, headers=HEADERS
            )
            assert bad.status_code == 422
            assert bad.json()["error"]["code"] == "INVALID_REQUEST"

            by_context = await client.get(
                "/api/v1/episodes",
                params={"scope": scope, "context_key": "feature:a"},
                headers=HEADERS,
            )
            assert {i["episode_id"] for i in by_context.json()["data"]["items"]} == {
                str(created[0].episode_id),
                str(created[1].episode_id),
                str(created[4].episode_id),
            }
            by_kind = await client.get(
                "/api/v1/episodes", params={"scope": scope, "kind": "decision"}, headers=HEADERS
            )
            assert {i["episode_id"] for i in by_kind.json()["data"]["items"]} == {
                str(created[0].episode_id),
                str(created[2].episode_id),
            }
            combined = await client.get(
                "/api/v1/episodes",
                params={"scope": scope, "context_key": "feature:a", "kind": "incident"},
                headers=HEADERS,
            )
            assert [i["episode_id"] for i in combined.json()["data"]["items"]] == [
                str(created[1].episode_id)
            ]
            unknown_project = await client.get(
                "/api/v1/episodes", params={"scope": new_scope()}, headers=HEADERS
            )
            assert unknown_project.json()["data"]["items"] == []
            global_scope = await client.get(
                "/api/v1/episodes", params={"scope": "global"}, headers=HEADERS
            )
            assert global_scope.json()["error"]["code"] == "INVALID_SCOPE"
    finally:
        await cleanup(session_factory, [scope, other_scope], set())


# --- semantic retrieval ---------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_internal_semantic_search_exact_cosine_order_and_exclusions(
    session_factory: AsyncSessionFactory,
) -> None:
    scope, other_scope = new_scope(), new_scope()
    vectors = {
        "query": axis_vector(1.0),
        "high": axis_vector(0.9),
        "mid-new": axis_vector(0.6),
        "mid-old": axis_vector(0.6),
        "tie-a": axis_vector(0.3),
        "tie-b": axis_vector(0.3),
        "invalidated": axis_vector(0.99),
        "forgotten": axis_vector(0.99),
        "foreign": axis_vector(0.99),
        "other-context": axis_vector(0.95),
    }
    fake = FakeEmbeddingClient(vectors)
    svc = service(session_factory, fake)
    day = datetime(2026, 11, 10, tzinfo=UTC)

    async def make(summary: str, *, at: datetime, target_scope: str = "", ck: str = "main") -> Any:
        return await svc.create(
            request(
                target_scope or scope,
                summary=summary,
                situation=None,
                reason=None,
                outcome=None,
                occurred_at=at.isoformat(),
                context_key=ck,
            ),
            idempotency_key=key(),
        )

    try:
        high = await make("high", at=day)
        mid_new = await make("mid-new", at=day + timedelta(days=1))
        mid_old = await make("mid-old", at=day - timedelta(days=1))
        tie_a = await make("tie-a", at=day)
        tie_b = await make("tie-b", at=day)
        invalidated = await make("invalidated", at=day)
        forgotten = await make("forgotten", at=day)
        await make("foreign", at=day, target_scope=other_scope)
        other_context = await make("other-context", at=day, ck="feature:other")
        await svc.invalidate(
            invalidated.episode_id, EpisodeInvalidateRequest(reason="x"), idempotency_key=key()
        )
        await svc.forget(forgotten.episode_id, idempotency_key=key())

        hits = await svc.semantic_search(scope=scope, query="query", limit=50)
        ties = sorted([tie_a, tie_b], key=lambda e: str(e.episode_id))
        assert [hit.episode.episode_id for hit in hits] == [
            other_context.episode_id,
            high.episode_id,
            mid_new.episode_id,
            mid_old.episode_id,
            *[e.episode_id for e in ties],
        ]
        relevances = [hit.relevance for hit in hits]
        assert relevances == sorted(relevances, reverse=True)
        assert relevances[1] == pytest.approx(0.9, abs=1e-5)
        excluded = {invalidated.episode_id, forgotten.episode_id}
        assert excluded.isdisjoint({hit.episode.episode_id for hit in hits})

        scoped = await svc.semantic_search(scope=scope, query="query", context_key="main", limit=2)
        assert [hit.episode.episode_id for hit in scoped] == [high.episode_id, mid_new.episode_id]
        assert fake.calls[-1] == ["query"]
    finally:
        await cleanup(session_factory, [scope, other_scope], set())


# --- embedding/provider boundary -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failure_persists_nothing_and_key_stays_usable(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    idem = key("failure")
    ledger_before = await count(session_factory, ContextualMemoryIdempotency)
    try:
        with pytest.raises(DependencyUnavailableError):
            await service(session_factory, FailingEmbeddingClient()).create(
                request(scope), idempotency_key=idem
            )
        async with http(build_app(session_factory, monkeypatch, FailingEmbeddingClient())) as c:
            response = await c.post(
                "/api/v1/episodes",
                json=payload(scope),
                headers={**HEADERS, "Idempotency-Key": idem},
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert await count(session_factory, Episode, Episode.scope == scope) == 0
        assert await count(session_factory, ContextualMemoryIdempotency) == ledger_before

        wrong_dimension = FakeEmbeddingClient({"x": [0.1] * 8})
        with pytest.raises(DependencyUnavailableError):
            await service(session_factory, wrong_dimension).create(
                request(scope, summary="x", situation=None, reason=None, outcome=None),
                idempotency_key=key(),
            )

        ok = await service(session_factory, FakeEmbeddingClient()).create(
            request(scope), idempotency_key=idem
        )
        assert ok.lifecycle.value == "active"
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_embedding_call_happens_outside_any_postgres_transaction() -> None:
    _contextual_episodes_test_database_url(dict(os.environ))
    settings = load_settings()
    application_name = f"ctx-episode-boundary-{uuid4().hex[:8]}"
    engine: AsyncEngine = create_async_engine(
        settings.database_url.get_secret_value(),
        connect_args={"server_settings": {"application_name": application_name}},
    )
    open_transactions = 0

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn: Any) -> None:
        nonlocal open_transactions
        open_transactions += 1

    @event.listens_for(engine.sync_engine, "commit")
    def _commit(conn: Any) -> None:
        nonlocal open_transactions
        open_transactions -= 1

    @event.listens_for(engine.sync_engine, "rollback")
    def _rollback(conn: Any) -> None:
        nonlocal open_transactions
        open_transactions -= 1

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    observer = create_async_engine_from_settings(settings)
    scope = new_scope()
    blocking = BlockingEmbeddingClient()
    svc = service(factory, blocking)
    task = asyncio.create_task(svc.create(request(scope), idempotency_key=key()))
    try:
        await asyncio.wait_for(blocking.started.wait(), timeout=10)
        assert open_transactions == 0
        async with observer.connect() as connection:
            busy = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name "
                    "AND state IN ('active', 'idle in transaction', "
                    "'idle in transaction (aborted)')"
                ),
                {"name": application_name},
            )
        assert busy == 0
        blocking.release.set()
        result = await asyncio.wait_for(task, timeout=10)
        assert result.lifecycle.value == "active"
        assert open_transactions == 0
    finally:
        blocking.release.set()
        if not task.done():
            task.cancel()
        await cleanup(factory, [scope], set())
        await engine.dispose()
        await observer.dispose()


# --- structural/DB guarantees ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_constraints_enforce_lifecycle_and_generic_ledger(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        episode = await svc.create(request(scope), idempotency_key=key())

        async def violates(statement: Any, params: dict[str, Any]) -> None:
            async with session_factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement), params)
                await session.rollback()

        await violates(
            "UPDATE episodes SET lifecycle = 'forgotten', forgotten_at = now() WHERE id = :id",
            {"id": episode.episode_id},
        )
        await violates(
            "UPDATE episodes SET lifecycle = 'invalidated', invalidated_at = now() WHERE id = :id",
            {"id": episode.episode_id},
        )
        await violates(
            "UPDATE episodes SET embedding = NULL WHERE id = :id", {"id": episode.episode_id}
        )
        await violates(
            "UPDATE episodes SET scope = 'global' WHERE id = :id", {"id": episode.episode_id}
        )
        await violates(
            "UPDATE episodes SET context_key = 'Main' WHERE id = :id", {"id": episode.episode_id}
        )
        await violates(
            "UPDATE episodes SET kind = 'note' WHERE id = :id", {"id": episode.episode_id}
        )
        await violates(
            "UPDATE episodes SET replacement_episode_id = id, lifecycle = 'invalidated', "
            "invalidated_at = now(), invalidation_reason = 'r' WHERE id = :id",
            {"id": episode.episode_id},
        )
        await violates(
            "INSERT INTO contextual_memory_idempotency (id, idempotency_key, operation, "
            "request_digest) VALUES (:id, 'raw-key', 'episode_create', :digest)",
            {"id": uuid4(), "digest": "a" * 64},
        )
        await violates(
            "INSERT INTO contextual_memory_idempotency (id, idempotency_key, operation, "
            "request_digest) VALUES (:id, :k, 'episode_forget', :digest)",
            {"id": uuid4(), "k": "b" * 64, "digest": "a" * 64},
        )

        # Resource-generic: a target/result UUID of a resource that is not an
        # Episode (e.g. a future checkpoint) is accepted -- no Episode FK.
        foreign_id = uuid4()
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO contextual_memory_idempotency (id, idempotency_key, operation, "
                    "request_digest, target_resource_id, result_resource_id) VALUES "
                    "(:id, :k, 'checkpoint_forget', :d, :t, :t)"
                ),
                {"id": uuid4(), "k": "c" * 63 + "1", "d": "d" * 64, "t": foreign_id},
            )
            fk_count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND "
                    "conrelid = 'contextual_memory_idempotency'::regclass"
                )
            )
            assert fk_count == 0
            await session.rollback()
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_lifecycle_has_no_graph_pipeline_cognitive_or_session_side_effects(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    before = await isolation_counts(session_factory)
    svc = service(session_factory, FakeEmbeddingClient())
    try:
        first = await svc.create(request(scope), idempotency_key=key())
        second = await svc.create(request(scope), idempotency_key=key())
        await svc.invalidate(
            first.episode_id,
            EpisodeInvalidateRequest(reason="r", replacement_episode_id=second.episode_id),
            idempotency_key=key(),
        )
        await svc.forget(first.episode_id, idempotency_key=key())
        await svc.forget(second.episode_id, idempotency_key=key())
        await svc.semantic_search(scope=scope, query="anything")
        assert await isolation_counts(session_factory) == before
    finally:
        await cleanup(session_factory, [scope], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_paths_never_log_content_refs_keys_or_secrets(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = new_scope()
    summary = f"SENTINEL-SUMMARY-{uuid4().hex}"
    reason = f"SENTINEL-REASON-{uuid4().hex}"
    invalidation_reason = f"SENTINEL-INVALIDATION-{uuid4().hex}"
    conversation_ref = f"SENTINEL-CONVERSATION-{uuid4().hex}"
    create_key = f"SENTINEL-KEY-CREATE-{uuid4().hex}"
    invalidate_key = f"SENTINEL-KEY-INVALIDATE-{uuid4().hex}"
    forget_key = f"SENTINEL-KEY-FORGET-{uuid4().hex}"
    app = build_app(session_factory, monkeypatch)
    stream = StringIO()
    clear_log_context()
    configure_logging("DEBUG", stream=stream)
    try:
        async with http(app) as client:
            body = payload(
                scope,
                summary=summary,
                reason=reason,
                provenance={
                    "origin_kind": "user_asserted",
                    "source_system": "manual-validation",
                    "conversation_ref": conversation_ref,
                },
            )
            created = await client.post(
                "/api/v1/episodes", json=body, headers={**HEADERS, "Idempotency-Key": create_key}
            )
            episode_id = created.json()["data"]["episode_id"]
            conflict = await client.post(
                "/api/v1/episodes",
                json={**body, "summary": summary + "-other"},
                headers={**HEADERS, "Idempotency-Key": create_key},
            )
            assert conflict.status_code == 409
            invalid = await client.post(
                "/api/v1/episodes",
                json={**body, "scope": "global"},
                headers={**HEADERS, "Idempotency-Key": key()},
            )
            assert invalid.status_code == 422
            await client.post(
                f"/api/v1/episodes/{episode_id}/invalidate",
                json={"reason": invalidation_reason},
                headers={**HEADERS, "Idempotency-Key": invalidate_key},
            )
            await client.post(
                f"/api/v1/episodes/{episode_id}/forget",
                headers={**HEADERS, "Idempotency-Key": forget_key},
            )
        async with session_factory() as session:
            digests = (
                await session.scalars(
                    select(ContextualMemoryIdempotency.request_digest).where(
                        ContextualMemoryIdempotency.result_resource_id == UUID(episode_id)
                    )
                )
            ).all()
        logged = stream.getvalue()
        for line in filter(None, logged.splitlines()):
            json.loads(line)
        assert "episode_created" in logged and "episode_forgotten" in logged
        for sentinel in (
            summary,
            reason,
            invalidation_reason,
            conversation_ref,
            create_key,
            invalidate_key,
            forget_key,
            HMAC_KEY,
            *digests,
        ):
            assert sentinel not in logged
    finally:
        configure_logging("INFO")
        await cleanup(session_factory, [scope], set())
