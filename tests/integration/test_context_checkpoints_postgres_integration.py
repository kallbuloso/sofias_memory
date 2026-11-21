"""Real-PostgreSQL integration suite for SM-CTX-002 Context Streams and
Initial Checkpoints (ADR-0017/ADR-0018, Contract v1 Contextual Project Memory,
handoff 10 SS 96-106).

Real PostgreSQL, the real ``ContextCheckpointService`` and the real HTTP app
are used; there is no provider to fake (checkpoints have no embedding) and
Neo4j is never needed.

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus
``DATABASE_URL`` pointing at a dedicated, discardable database already migrated
to head (0020) -- never a shared development database. Every test uses its
own random Project Scope and removes the rows it created.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from io import StringIO
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.domain import context_stream_lock_key, protect_contextual_idempotency_key
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    ContextCheckpoint,
    ContextCheckpointProvenance,
    ContextCheckpointStatement,
    ContextualMemoryIdempotency,
    Dataset,
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
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointInvalidateRequest,
    ContextCheckpointResult,
)
from sofias_memory.services.context_checkpoints import ContextCheckpointService
from tests.unit._app_factory import create_app

CONTEXT_CHECKPOINTS_POSTGRES_ENV = "SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}
BASE = "/api/v1/context/checkpoints"


# --- fixtures/helpers -----------------------------------------------------------


def _test_database_url(env: dict[str, str]) -> str:
    if env.get(CONTEXT_CHECKPOINTS_POSTGRES_ENV) != "1":
        pytest.skip(f"set {CONTEXT_CHECKPOINTS_POSTGRES_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


def test_context_checkpoints_suite_skips_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _test_database_url({})


@pytest_asyncio.fixture()
async def session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _test_database_url(dict(os.environ))
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


def service(factory: AsyncSessionFactory) -> ContextCheckpointService:
    return ContextCheckpointService(make_settings(), session_factory=factory)


def build_app(factory: AsyncSessionFactory) -> Any:
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=factory,
    )


def http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def new_scope() -> str:
    return f"project:ctx-cp-{uuid4().hex[:12]}"


def key(prefix: str = "k") -> str:
    return f"{prefix}-{uuid4().hex}"


def keyed(idempotency_key: str | None = None) -> dict[str, str]:
    return {**HEADERS, "Idempotency-Key": idempotency_key or key()}


def payload(scope: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scope": scope,
        "context_key": "feature:v1",
        "objective": {"text": "Implementar memória contextual persistente."},
        "current_state": {"text": f"PRD, Spec e Contract estão aprovados {uuid4().hex}."},
        "approved_decisions": [{"text": "Episode será first-class."}],
        "pending_work": [
            {"text": "Implementar persistence."},
            {"text": "Implementar API."},
            {"text": "Executar Human Validation."},
        ],
        "open_items": [],
        "blockers": [{"text": "Nenhum bloqueio estrutural."}],
        "next_action": {"text": "Implementar SM-CTX-002."},
        "provenance": {
            "origin_kind": "assistant_generated",
            "source_system": "sofias-personal-assistant",
            "conversation_ref": "conv-sm-ctx-002",
            "turn_ref": "turn-1",
        },
    }
    body.update(overrides)
    return body


def request(scope: str, **overrides: Any) -> ContextCheckpointCreateRequest:
    return ContextCheckpointCreateRequest.model_validate(payload(scope, **overrides))


async def cleanup(factory: AsyncSessionFactory, scopes: Sequence[str]) -> None:
    async with factory() as session:
        ids = set(
            (
                await session.scalars(
                    select(ContextCheckpoint.id).where(ContextCheckpoint.scope.in_(list(scopes)))
                )
            ).all()
        )
        if ids:
            await session.execute(
                delete(ContextualMemoryIdempotency).where(
                    ContextualMemoryIdempotency.result_resource_id.in_(ids)
                    | ContextualMemoryIdempotency.target_resource_id.in_(ids)
                )
            )
            await session.execute(delete(ContextCheckpoint).where(ContextCheckpoint.id.in_(ids)))
        await session.commit()


async def count(factory: AsyncSessionFactory, model: type, *where: Any) -> int:
    async with factory() as session:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(await session.scalar(statement) or 0)


async def stream_rows(
    factory: AsyncSessionFactory, scope: str, context_key: str
) -> list[ContextCheckpoint]:
    async with factory() as session:
        rows = await session.scalars(
            select(ContextCheckpoint)
            .where(ContextCheckpoint.scope == scope, ContextCheckpoint.context_key == context_key)
            .order_by(ContextCheckpoint.revision)
        )
        return list(rows.all())


def ledger_count_for(idempotency_key: str) -> Any:
    protected = protect_contextual_idempotency_key(
        secret=HMAC_KEY.encode(), idempotency_key=idempotency_key
    )
    return ContextualMemoryIdempotency.idempotency_key == protected


ISOLATION_MODELS: tuple[type, ...] = (
    Episode,
    EpisodeProvenance,
    MemoryItem,
    MemoryProvenance,
    CognitiveMemoryIdempotency,
    Session,
    SessionEntry,
    Dataset,
    PipelineRun,
    PipelineStep,
    GraphOutbox,
)


async def isolation_counts(factory: AsyncSessionFactory) -> dict[str, int]:
    return {model.__name__: await count(factory, model) for model in ISOLATION_MODELS}


def assert_conflict(error: BaseException, code: str) -> None:
    assert isinstance(error, SofiasMemoryError), error
    assert error.code.value == code


def _advisory_lock_ids(lock_key: int) -> tuple[int, int]:
    unsigned = lock_key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


async def run_contended(
    scope: str, context_key: str, *coroutines: Any
) -> list[ContextCheckpointResult | BaseException]:
    """Deterministic contention: a third connection holds the stream's
    advisory lock while every coroutine starts and is observed *waiting* on it
    in ``pg_locks``; only then is the lock released, so all contenders truly
    race for the same stream instead of running one after the other."""

    lock_key = context_stream_lock_key(scope, context_key)
    classid, objid = _advisory_lock_ids(lock_key)
    engine = create_async_engine(_test_database_url(dict(os.environ)))
    try:
        async with engine.connect() as holder:
            # SQLAlchemy autobegins: the lock is held by this open transaction
            # until holder.commit() below.
            await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})
            tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
            async with engine.connect() as probe:
                for _ in range(200):
                    waiting = await probe.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND NOT granted AND classid = :c AND objid = :o AND objsubid = 1"
                        ),
                        {"c": classid, "o": objid},
                    )
                    await probe.rollback()
                    if waiting == len(tasks):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("contenders never queued on the stream advisory lock")
            await holder.commit()
        return list(await asyncio.gather(*tasks, return_exceptions=True))
    finally:
        await engine.dispose()


# --- create / get / current -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_create_default_main_revision_1_get_current_and_order(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        async with http(build_app(session_factory)) as client:
            body = payload(scope)
            del body["context_key"]
            created = await client.post(BASE, json=body, headers=keyed())
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            assert data["context_key"] == "main"
            assert data["revision"] == 1 and data["lifecycle"] == "active"
            assert data["scope"] == scope
            assert data["current_state"]["text"] == body["current_state"]["text"]
            assert [item["text"] for item in data["pending_work"]] == [
                "Implementar persistence.",
                "Implementar API.",
                "Executar Human Validation.",
            ]
            assert data["open_items"] == []
            statement_ids = [
                data["objective"]["statement_id"],
                data["current_state"]["statement_id"],
                data["next_action"]["statement_id"],
                *[item["statement_id"] for item in data["pending_work"]],
            ]
            assert len(set(statement_ids)) == len(statement_ids)
            assert data["provenance"] == body["provenance"]
            assert data["superseded_by"] is None and data["invalidated_at"] is None

            got = await client.get(f"{BASE}/{data['checkpoint_id']}", headers=HEADERS)
            assert got.status_code == 200 and got.json()["data"] == data

            current = await client.get(f"{BASE}/current", params={"scope": scope}, headers=HEADERS)
            assert current.status_code == 200 and current.json()["data"] == data
            explicit = await client.get(
                f"{BASE}/current", params={"scope": scope, "context_key": "main"}, headers=HEADERS
            )
            assert explicit.json()["data"] == data

            missing = await client.get(f"{BASE}/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "CHECKPOINT_NOT_FOUND"
            none = await client.get(
                f"{BASE}/current", params={"scope": scope, "context_key": "other"}, headers=HEADERS
            )
            assert none.status_code == 404
            assert none.json()["error"]["code"] == "CURRENT_CHECKPOINT_NOT_FOUND"
            client_id = await client.post(
                BASE, json={**payload(scope), "checkpoint_id": str(uuid4())}, headers=keyed()
            )
            assert client_id.status_code == 422
            no_state = payload(scope, context_key="feature:no-state")
            del no_state["current_state"]
            rejected = await client.post(BASE, json=no_state, headers=keyed())
            assert rejected.status_code == 422

        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(ContextCheckpointStatement)
                    .where(ContextCheckpointStatement.checkpoint_id == UUID(data["checkpoint_id"]))
                    .order_by(
                        ContextCheckpointStatement.statement_type,
                        ContextCheckpointStatement.ordinal,
                    )
                )
            ).all()
            current_states = [r for r in rows if r.statement_type.value == "current_state"]
            assert len(current_states) == 1
            assert [r.ordinal for r in rows if r.statement_type.value == "pending_work"] == [
                0,
                1,
                2,
            ]
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == scope) == 1
        )
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_while_active_conflicts_and_replay_takes_precedence(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    original_key = key("orig")
    try:
        async with http(build_app(session_factory)) as client:
            body = payload(scope)
            created = await client.post(BASE, json=body, headers=keyed(original_key))
            assert created.status_code == 201
            original = created.json()["data"]

            duplicate = await client.post(BASE, json=payload(scope), headers=keyed())
            assert duplicate.status_code == 409
            error = duplicate.json()["error"]
            assert error["code"] == "CONTEXT_STATE_CONFLICT"
            assert error["details"]["current_checkpoint_id"] == original["checkpoint_id"]

            replay = await client.post(BASE, json=body, headers=keyed(original_key))
            assert replay.status_code == 201
            assert replay.json()["data"] == original

            changed = await client.post(
                BASE,
                json={**body, "current_state": {"text": "outro"}},
                headers=keyed(original_key),
            )
            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            reserved = await client.post(BASE, json=payload(scope), headers=keyed("sys:reserved"))
            assert reserved.status_code == 400

        rows = await stream_rows(session_factory, scope, "feature:v1")
        assert [(row.revision, row.lifecycle.value) for row in rows] == [(1, "active")]
        assert (
            await count(
                session_factory, ContextualMemoryIdempotency, ledger_count_for(original_key)
            )
            == 1
        )
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_is_bound_to_operation_across_resources(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        create_key = key("shared")
        created = await svc.create_current(request(scope), idempotency_key=create_key)
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.invalidate(
                created.checkpoint_id,
                ContextCheckpointInvalidateRequest(reason="r"),
                idempotency_key=create_key,
            )
        assert_conflict(caught.value, "IDEMPOTENCY_CONFLICT")
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.forget(created.checkpoint_id, idempotency_key=create_key)
        assert_conflict(caught.value, "IDEMPOTENCY_CONFLICT")
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.create_current(
                request(scope, context_key="feature:other"), idempotency_key=create_key
            )
        assert_conflict(caught.value, "IDEMPOTENCY_CONFLICT")
        assert (await svc.get(created.checkpoint_id)).lifecycle.value == "active"
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == scope) == 1
        )
    finally:
        await cleanup(session_factory, [scope])


# --- lifecycle --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_then_recovery_create_allocates_next_revision_without_lineage(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        async with http(build_app(session_factory)) as client:
            first = (await client.post(BASE, json=payload(scope), headers=keyed())).json()["data"]
            first_id = first["checkpoint_id"]
            invalidate_key = key("inv")
            invalidated = await client.post(
                f"{BASE}/{first_id}/invalidate",
                json={"reason": "Checkpoint is no longer considered reliable."},
                headers=keyed(invalidate_key),
            )
            assert invalidated.status_code == 200
            inv = invalidated.json()["data"]
            assert inv["lifecycle"] == "invalidated" and inv["invalidated_at"] is not None
            assert inv["current_state"] == first["current_state"]
            assert "invalidation_reason" not in inv

            replay = await client.post(
                f"{BASE}/{first_id}/invalidate",
                json={"reason": "Checkpoint is no longer considered reliable."},
                headers=keyed(invalidate_key),
            )
            assert replay.status_code == 200 and replay.json()["data"] == inv
            no_op = await client.post(
                f"{BASE}/{first_id}/invalidate", json={"reason": "other"}, headers=keyed()
            )
            assert no_op.status_code == 200
            assert no_op.json()["data"]["invalidated_at"] == inv["invalidated_at"]

            current = await client.get(
                f"{BASE}/current",
                params={"scope": scope, "context_key": "feature:v1"},
                headers=HEADERS,
            )
            assert current.status_code == 404
            assert current.json()["error"]["code"] == "CURRENT_CHECKPOINT_NOT_FOUND"
            active_list = await client.get(BASE, params={"scope": scope}, headers=HEADERS)
            assert active_list.json()["data"]["items"] == []
            history = await client.get(
                BASE, params={"scope": scope, "lifecycle": "invalidated"}, headers=HEADERS
            )
            assert [i["checkpoint_id"] for i in history.json()["data"]["items"]] == [first_id]

            recovered = await client.post(BASE, json=payload(scope), headers=keyed())
            assert recovered.status_code == 201
            second = recovered.json()["data"]
            assert second["revision"] == 2 and second["lifecycle"] == "active"
            assert second["checkpoint_id"] != first_id
            assert {s["statement_id"] for s in second["pending_work"]}.isdisjoint(
                {s["statement_id"] for s in first["pending_work"]}
            )
            old = (await client.get(f"{BASE}/{first_id}", headers=HEADERS)).json()["data"]
            assert old["lifecycle"] == "invalidated"
            assert old["superseded_by"] is None and old["superseded_at"] is None

        async with session_factory() as session:
            row = await session.get(ContextCheckpoint, UUID(first_id))
            assert row is not None
            assert row.invalidation_reason == "Checkpoint is no longer considered reliable."
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_produces_scrubbed_tombstone_and_recovery_create_continues_sequence(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        async with http(build_app(session_factory)) as client:
            first = (await client.post(BASE, json=payload(scope), headers=keyed())).json()["data"]
            await client.post(
                f"{BASE}/{first['checkpoint_id']}/invalidate",
                json={"reason": "SECRET-REASON"},
                headers=keyed(),
            )
            second = (await client.post(BASE, json=payload(scope), headers=keyed())).json()["data"]
            assert second["revision"] == 2

            forget_key = key("forget")
            forgotten = await client.post(
                f"{BASE}/{second['checkpoint_id']}/forget", headers=keyed(forget_key)
            )
            assert forgotten.status_code == 200
            tomb = forgotten.json()["data"]
            assert tomb["lifecycle"] == "forgotten" and tomb["forgotten_at"] is not None
            assert (tomb["scope"], tomb["context_key"], tomb["revision"]) == (
                scope,
                "feature:v1",
                2,
            )
            assert tomb["created_at"] == second["created_at"]
            for field in ("objective", "current_state", "next_action"):
                assert tomb[field] is None
            for field in ("approved_decisions", "pending_work", "open_items", "blockers"):
                assert tomb[field] == []
            assert tomb["provenance"] == {
                "origin_kind": "assistant_generated",
                "source_system": "sofias-personal-assistant",
                "conversation_ref": None,
                "turn_ref": None,
            }
            again = await client.post(f"{BASE}/{second['checkpoint_id']}/forget", headers=keyed())
            assert again.status_code == 200 and again.json()["data"] == tomb
            replay = await client.post(
                f"{BASE}/{second['checkpoint_id']}/forget", headers=keyed(forget_key)
            )
            assert replay.json()["data"] == tomb
            invalidate_forgotten = await client.post(
                f"{BASE}/{second['checkpoint_id']}/invalidate",
                json={"reason": "r"},
                headers=keyed(),
            )
            assert invalidate_forgotten.status_code == 409
            assert invalidate_forgotten.json()["error"]["code"] == "CHECKPOINT_STATE_CONFLICT"

            current = await client.get(
                f"{BASE}/current",
                params={"scope": scope, "context_key": "feature:v1"},
                headers=HEADERS,
            )
            assert current.status_code == 404

            # Forget the INVALIDATED revision too: its reason must be scrubbed.
            first_forgotten = await client.post(
                f"{BASE}/{first['checkpoint_id']}/forget", headers=keyed()
            )
            assert first_forgotten.json()["data"]["invalidated_at"] is not None

            third = (await client.post(BASE, json=payload(scope), headers=keyed())).json()["data"]
            assert third["revision"] == 3 and third["lifecycle"] == "active"

        async with session_factory() as session:
            for checkpoint_id in (first["checkpoint_id"], second["checkpoint_id"]):
                cid = UUID(checkpoint_id)
                row = await session.get(ContextCheckpoint, cid)
                assert row is not None and row.lifecycle.value == "forgotten"
                assert row.invalidation_reason is None and row.superseded_by is None
                assert row.scope == scope and row.context_key == "feature:v1"
                statements = await session.scalar(
                    select(func.count())
                    .select_from(ContextCheckpointStatement)
                    .where(ContextCheckpointStatement.checkpoint_id == cid)
                )
                assert statements == 0
                provenance = await session.get(ContextCheckpointProvenance, cid)
                assert provenance is not None
                assert provenance.conversation_ref is None and provenance.turn_ref is None
            dump = json.dumps(
                [
                    list(r)
                    for r in (
                        await session.execute(
                            text(
                                "SELECT c.*, p.* FROM context_checkpoints c JOIN "
                                "context_checkpoint_provenance p ON p.checkpoint_id = c.id "
                                "WHERE c.id = ANY(:ids)"
                            ),
                            {"ids": [UUID(first["checkpoint_id"]), UUID(second["checkpoint_id"])]},
                        )
                    ).all()
                ],
                default=str,
            )
            for sentinel in (
                "SECRET-REASON",
                "conv-sm-ctx-002",
                "turn-1",
                second["current_state"]["text"],
            ):
                assert sentinel not in dump
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_every_revision_restart_then_create_uses_max_plus_one(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        first = await svc.create_current(request(scope), idempotency_key=key())
        await svc.forget(first.checkpoint_id, idempotency_key=key())
        second = await svc.create_current(request(scope), idempotency_key=key())
        assert second.revision == 2
        await svc.forget(second.checkpoint_id, idempotency_key=key())
        rows = await stream_rows(session_factory, scope, "feature:v1")
        assert [(r.revision, r.lifecycle.value) for r in rows] == [
            (1, "forgotten"),
            (2, "forgotten"),
        ]

        engine = create_async_engine_from_settings(load_settings())
        try:
            restarted = service(create_session_factory(engine))
            third = await restarted.create_current(request(scope), idempotency_key=key())
            assert third.revision == 3 and third.lifecycle.value == "active"
        finally:
            await dispose_async_engine(engine)
        async with session_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO context_checkpoints (id, scope, context_key, revision, "
                        "lifecycle, forgotten_at) VALUES (:id, :s, 'feature:v1', 1, "
                        "'forgotten', now())"
                    ),
                    {"id": uuid4(), "s": scope},
                )
            await session.rollback()
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_state_survives_restart_with_new_engine_app_and_process() -> None:
    _test_database_url(dict(os.environ))
    scope = new_scope()
    engine_one = create_async_engine_from_settings(load_settings())
    factory_one = create_session_factory(engine_one)
    try:
        async with http(build_app(factory_one)) as client:
            main = (
                await client.post(
                    BASE, json=payload(scope, context_key="feature:memory"), headers=keyed()
                )
            ).json()["data"]
            historical = (
                await client.post(
                    BASE, json=payload(scope, context_key="feature:h"), headers=keyed()
                )
            ).json()["data"]
            await client.post(
                f"{BASE}/{historical['checkpoint_id']}/invalidate",
                json={"reason": "r"},
                headers=keyed(),
            )
            current_h = (
                await client.post(
                    BASE, json=payload(scope, context_key="feature:h"), headers=keyed()
                )
            ).json()["data"]
            assert current_h["revision"] == 2
    finally:
        await dispose_async_engine(engine_one)

    engine_two = create_async_engine_from_settings(load_settings())
    factory_two = create_session_factory(engine_two)
    try:
        async with http(build_app(factory_two)) as client:
            got = await client.get(
                f"{BASE}/current",
                params={"scope": scope, "context_key": "feature:memory"},
                headers=HEADERS,
            )
            assert got.status_code == 200 and got.json()["data"] == main
            got_h = await client.get(
                f"{BASE}/current",
                params={"scope": scope, "context_key": "feature:h"},
                headers=HEADERS,
            )
            assert got_h.json()["data"] == current_h
            old = (
                await client.get(f"{BASE}/{historical['checkpoint_id']}", headers=HEADERS)
            ).json()
            assert old["data"]["lifecycle"] == "invalidated" and old["data"]["revision"] == 1

        child = (
            "import asyncio\n"
            "from sofias_memory.config import load_settings\n"
            "from sofias_memory.infrastructure.postgres import create_async_engine_from_settings,"
            " create_session_factory, dispose_async_engine\n"
            "from sofias_memory.services.context_checkpoints import ContextCheckpointService\n"
            "async def main():\n"
            "    settings = load_settings()\n"
            "    engine = create_async_engine_from_settings(settings)\n"
            "    try:\n"
            "        svc = ContextCheckpointService(settings,"
            " session_factory=create_session_factory(engine))\n"
            f"        result = await svc.get_current(scope='{scope}',"
            " context_key='feature:memory')\n"
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
        assert from_new_process["checkpoint_id"] == main["checkpoint_id"]
        assert from_new_process["current_state"] == main["current_state"]
        assert from_new_process["pending_work"] == main["pending_work"]
        assert from_new_process["revision"] == 1
    finally:
        await cleanup(factory_two, [scope])
        await dispose_async_engine(engine_two)


# --- streams / isolation / listing -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_streams_and_project_isolation(
    session_factory: AsyncSessionFactory,
) -> None:
    scope_a, scope_b = new_scope(), new_scope()
    svc = service(session_factory)
    try:
        created = {
            ck: await svc.create_current(request(scope_a, context_key=ck), idempotency_key=key())
            for ck in ("main", "feature:a", "feature:b")
        }
        other = await svc.create_current(
            request(scope_b, context_key="main"), idempotency_key=key()
        )
        for ck, result in created.items():
            assert result.revision == 1
            current = await svc.get_current(scope=scope_a, context_key=ck)
            assert current.checkpoint_id == result.checkpoint_id
        assert (await svc.get_current(scope=scope_b, context_key="main")).checkpoint_id == (
            other.checkpoint_id
        )
        assert other.checkpoint_id != created["main"].checkpoint_id
        await svc.invalidate(
            created["main"].checkpoint_id,
            ContextCheckpointInvalidateRequest(reason="r"),
            idempotency_key=key(),
        )
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.get_current(scope=scope_a, context_key="main")
        assert_conflict(caught.value, "CURRENT_CHECKPOINT_NOT_FOUND")
        assert (
            await svc.get_current(scope=scope_b, context_key="main")
        ).lifecycle.value == "active"
        assert (await svc.get_current(scope=scope_a, context_key="feature:a")).lifecycle.value == (
            "active"
        )
    finally:
        await cleanup(session_factory, [scope_a, scope_b])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_listing_order_filters_and_cursor_pagination(
    session_factory: AsyncSessionFactory,
) -> None:
    scope, foreign = new_scope(), new_scope()
    svc = service(session_factory)
    try:
        for _ in range(3):
            current = await svc.create_current(
                request(scope, context_key="b"), idempotency_key=key()
            )
            await svc.invalidate(
                current.checkpoint_id,
                ContextCheckpointInvalidateRequest(reason="r"),
                idempotency_key=key(),
            )
        for _ in range(2):
            current = await svc.create_current(
                request(scope, context_key="a"), idempotency_key=key()
            )
            await svc.invalidate(
                current.checkpoint_id,
                ContextCheckpointInvalidateRequest(reason="r"),
                idempotency_key=key(),
            )
        await svc.create_current(request(scope, context_key="a"), idempotency_key=key())
        await svc.create_current(request(foreign, context_key="a"), idempotency_key=key())

        everything = await svc.list_checkpoints(
            scope=scope, context_key=None, lifecycle=None, limit=50, cursor=None
        )
        assert [(i.context_key, i.revision) for i in everything.items] == [("a", 3)]

        pages: list[tuple[str, int]] = []
        cursor: str | None = None
        while True:
            page = await svc.list_checkpoints(
                scope=scope,
                context_key=None,
                lifecycle="invalidated",
                limit=2,
                cursor=cursor,  # type: ignore[arg-type]
            )
            assert len(page.items) <= 2
            pages.extend((i.context_key, i.revision) for i in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert pages == [("a", 2), ("a", 1), ("b", 3), ("b", 2), ("b", 1)]

        only_b = await svc.list_checkpoints(
            scope=scope,
            context_key="b",
            lifecycle="invalidated",
            limit=50,
            cursor=None,  # type: ignore[arg-type]
        )
        assert [i.revision for i in only_b.items] == [3, 2, 1]

        async with http(build_app(session_factory)) as client:
            bad_cursor = await client.get(
                BASE, params={"scope": scope, "cursor": "not-a-cursor"}, headers=HEADERS
            )
            assert bad_cursor.status_code == 422
            assert bad_cursor.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup(session_factory, [scope, foreign])


# --- races ------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_brand_new_stream_create_race_has_exactly_one_winner(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    try:
        results = await run_contended(
            scope,
            "feature:v1",
            service(session_factory).create_current(request(scope), idempotency_key=key("a")),
            service(session_factory).create_current(request(scope), idempotency_key=key("b")),
        )
        winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert_conflict(losers[0], "CONTEXT_STATE_CONFLICT")
        assert winners[0].revision == 1
        rows = await stream_rows(session_factory, scope, "feature:v1")
        assert [(r.revision, r.lifecycle.value) for r in rows] == [(1, "active")]
        assert (
            await count(
                session_factory,
                ContextCheckpointProvenance,
                ContextCheckpointProvenance.checkpoint_id == winners[0].checkpoint_id,
            )
            == 1
        )
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_brand_new_stream_uncoordinated_race_never_creates_two_actives(
    session_factory: AsyncSessionFactory,
) -> None:
    scopes = [new_scope() for _ in range(10)]
    try:
        for scope in scopes:
            results = await asyncio.gather(
                *[
                    service(session_factory).create_current(request(scope), idempotency_key=key())
                    for _ in range(4)
                ],
                return_exceptions=True,
            )
            winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
            assert len(winners) == 1
            for loser in (r for r in results if isinstance(r, BaseException)):
                assert_conflict(loser, "CONTEXT_STATE_CONFLICT")
            rows = await stream_rows(session_factory, scope, "feature:v1")
            assert [(r.revision, r.lifecycle.value) for r in rows] == [(1, "active")]
    finally:
        await cleanup(session_factory, scopes)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_create_race_has_one_winner_and_no_extra_revision(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        first = await svc.create_current(request(scope), idempotency_key=key())
        await svc.invalidate(
            first.checkpoint_id,
            ContextCheckpointInvalidateRequest(reason="r"),
            idempotency_key=key(),
        )
        results = await run_contended(
            scope,
            "feature:v1",
            service(session_factory).create_current(request(scope), idempotency_key=key("a")),
            service(session_factory).create_current(request(scope), idempotency_key=key("b")),
            service(session_factory).create_current(request(scope), idempotency_key=key("c")),
        )
        winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 2
        for loser in losers:
            assert_conflict(loser, "CONTEXT_STATE_CONFLICT")
        assert winners[0].revision == 2
        rows = await stream_rows(session_factory, scope, "feature:v1")
        assert [(r.revision, r.lifecycle.value) for r in rows] == [
            (1, "invalidated"),
            (2, "active"),
        ]
        assert rows[0].superseded_by is None
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_concurrent_create_converges_without_orphans(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    idem = key("same")
    body = request(scope)
    try:
        results = await run_contended(
            scope,
            "feature:v1",
            service(session_factory).create_current(body, idempotency_key=idem),
            service(session_factory).create_current(body, idempotency_key=idem),
        )
        assert all(isinstance(r, ContextCheckpointResult) for r in results), results
        first, second = results
        assert isinstance(first, ContextCheckpointResult)
        assert isinstance(second, ContextCheckpointResult)
        assert first.checkpoint_id == second.checkpoint_id
        assert first == second
        rows = await stream_rows(session_factory, scope, "feature:v1")
        assert len(rows) == 1 and rows[0].revision == 1
        assert (
            await count(
                session_factory,
                ContextCheckpointStatement,
                ContextCheckpointStatement.checkpoint_id == first.checkpoint_id,
            )
            == 8  # objective + current_state + next_action + 1 decision + 3 pending + 1 blocker
        )
        assert (
            await count(
                session_factory,
                ContextCheckpointProvenance,
                ContextCheckpointProvenance.checkpoint_id == first.checkpoint_id,
            )
            == 1
        )
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 1
        )
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_different_streams_concurrently_one_wins_one_conflicts(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    idem = key("cross-stream")
    try:
        results = await asyncio.gather(
            service(session_factory).create_current(
                request(scope, context_key="x"), idempotency_key=idem
            ),
            service(session_factory).create_current(
                request(scope, context_key="y"), idempotency_key=idem
            ),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, ContextCheckpointResult)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert_conflict(losers[0], "IDEMPOTENCY_CONFLICT")
        assert (
            await count(session_factory, ContextCheckpoint, ContextCheckpoint.scope == scope) == 1
        )
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 1
        )
    finally:
        await cleanup(session_factory, [scope])


# --- database protection -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_constraints_protect_single_active_revision_and_shape(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    svc = service(session_factory)
    try:
        current = await svc.create_current(request(scope), idempotency_key=key())
        cid = current.checkpoint_id

        async def violates(statement: str, params: dict[str, Any]) -> None:
            async with session_factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement), params)
                await session.rollback()

        insert = (
            "INSERT INTO context_checkpoints (id, scope, context_key, revision, lifecycle) "
            "VALUES (:id, :s, :k, :r, 'active')"
        )
        # Second ACTIVE in the same stream (different revision): partial unique index.
        await violates(insert, {"id": uuid4(), "s": scope, "k": "feature:v1", "r": 2})
        # Duplicate revision (even non-active): revision unique constraint.
        await violates(
            "INSERT INTO context_checkpoints (id, scope, context_key, revision, lifecycle, "
            "invalidated_at, invalidation_reason) VALUES (:id, :s, 'feature:v1', 1, "
            "'invalidated', now(), 'r')",
            {"id": uuid4(), "s": scope},
        )
        await violates(insert, {"id": uuid4(), "s": "global", "k": "main", "r": 1})
        await violates(insert, {"id": uuid4(), "s": scope, "k": "Main", "r": 1})
        await violates(insert, {"id": uuid4(), "s": scope, "k": "fresh", "r": 0})
        await violates("UPDATE context_checkpoints SET scope = NULL WHERE id = :id", {"id": cid})
        await violates(
            "UPDATE context_checkpoints SET lifecycle = 'forgotten' WHERE id = :id", {"id": cid}
        )
        await violates(
            "UPDATE context_checkpoints SET lifecycle = 'invalidated', invalidated_at = now() "
            "WHERE id = :id",
            {"id": cid},
        )
        await violates(
            "UPDATE context_checkpoints SET lifecycle = 'superseded' WHERE id = :id", {"id": cid}
        )
        await violates(
            "UPDATE context_checkpoints SET lifecycle = 'forgotten', forgotten_at = now(), "
            "invalidated_at = now(), invalidation_reason = 'kept' WHERE id = :id",
            {"id": cid},
        )
        stmt_insert = (
            "INSERT INTO context_checkpoint_statements (id, checkpoint_id, statement_type, "
            "ordinal, content) VALUES (:id, :c, :t, :o, :x)"
        )
        await violates(
            stmt_insert, {"id": uuid4(), "c": cid, "t": "current_state", "o": 0, "x": "dup"}
        )
        await violates(stmt_insert, {"id": uuid4(), "c": cid, "t": "objective", "o": 1, "x": "x"})
        await violates(
            stmt_insert, {"id": uuid4(), "c": cid, "t": "pending_work", "o": 0, "x": "x"}
        )
        await violates(stmt_insert, {"id": uuid4(), "c": cid, "t": "blocker", "o": -1, "x": "x"})
        await violates(stmt_insert, {"id": uuid4(), "c": cid, "t": "open_item", "o": 0, "x": "  "})

        async with session_factory() as session:
            definition = await session.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = "
                    "'uq_context_checkpoints_active_stream'"
                )
            )
        normalized = " ".join(str(definition).lower().split())
        assert "create unique index" in normalized
        assert "(scope, context_key)" in normalized
        assert "where (lifecycle = 'active'" in normalized
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_context_streams_or_projects_tables_exist(
    session_factory: AsyncSessionFactory,
) -> None:
    async with session_factory() as session:
        tables = set(
            (
                await session.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            ).all()
        )
    assert {"context_checkpoints", "context_checkpoint_statements"} <= tables
    assert "context_checkpoint_provenance" in tables
    assert not {"context_streams", "projects", "context_evidence", "evidence"} & tables


# --- cross-feature / privacy ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_lifecycle_has_no_cross_feature_side_effects(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    before = await isolation_counts(session_factory)
    svc = service(session_factory)
    try:
        first = await svc.create_current(request(scope), idempotency_key=key())
        await svc.get_current(scope=scope, context_key="feature:v1")
        await svc.invalidate(
            first.checkpoint_id,
            ContextCheckpointInvalidateRequest(reason="r"),
            idempotency_key=key(),
        )
        second = await svc.create_current(request(scope), idempotency_key=key())
        await svc.forget(second.checkpoint_id, idempotency_key=key())
        await svc.forget(first.checkpoint_id, idempotency_key=key())
        await svc.list_checkpoints(
            scope=scope, context_key=None, lifecycle=None, limit=10, cursor=None
        )
        assert await isolation_counts(session_factory) == before
    finally:
        await cleanup(session_factory, [scope])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_paths_never_log_content_refs_keys_or_secrets(
    session_factory: AsyncSessionFactory,
) -> None:
    scope = new_scope()
    state = f"SENTINEL-STATE-{uuid4().hex}"
    pending = f"SENTINEL-PENDING-{uuid4().hex}"
    reason = f"SENTINEL-REASON-{uuid4().hex}"
    conversation_ref = f"SENTINEL-CONVERSATION-{uuid4().hex}"
    keys = [f"SENTINEL-KEY-{name}-{uuid4().hex}" for name in ("create", "inv", "forget", "dup")]
    stream = StringIO()
    clear_log_context()
    configure_logging("DEBUG", stream=stream)
    try:
        async with http(build_app(session_factory)) as client:
            body = payload(
                scope,
                current_state={"text": state},
                pending_work=[{"text": pending}],
                provenance={
                    "origin_kind": "user_asserted",
                    "source_system": "manual-validation",
                    "conversation_ref": conversation_ref,
                },
            )
            created = await client.post(BASE, json=body, headers=keyed(keys[0]))
            checkpoint_id = created.json()["data"]["checkpoint_id"]
            duplicate = await client.post(BASE, json=body, headers=keyed(keys[3]))
            assert duplicate.status_code == 409
            await client.post(
                f"{BASE}/{checkpoint_id}/invalidate",
                json={"reason": reason},
                headers=keyed(keys[1]),
            )
            await client.post(f"{BASE}/{checkpoint_id}/forget", headers=keyed(keys[2]))
        async with session_factory() as session:
            digests = (
                await session.scalars(
                    select(ContextualMemoryIdempotency.request_digest).where(
                        ContextualMemoryIdempotency.result_resource_id == UUID(checkpoint_id)
                    )
                )
            ).all()
        logged = stream.getvalue()
        for line in filter(None, logged.splitlines()):
            json.loads(line)
        for event in (
            "checkpoint_created",
            "checkpoint_conflict",
            "checkpoint_invalidated",
            "checkpoint_forgotten",
        ):
            assert event in logged
        for sentinel in (state, pending, reason, conversation_ref, *keys, HMAC_KEY, *digests):
            assert sentinel not in logged
    finally:
        configure_logging("INFO")
        await cleanup(session_factory, [scope])
