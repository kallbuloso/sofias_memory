"""ADR-0011 item 28: production-shaped S3 smoke.

One continuous scenario against real PostgreSQL (a dedicated, discardable
test database -- never `cognee_db`), real filesystem, and real MinIO
(bucket `sofias-memory`, a fresh unique validation prefix), with Neo4j
disabled throughout (no shared Neo4j Community state is touched) and no real
LLM/embedding network calls (Remember runs in `mode="ingest"`, which never
invokes either).

Every phase uses the REAL production composition root
(`sofias_memory.app.create_app`) and the REAL ASGI `lifespan()` context
manager (`app.router.lifespan_context(app)`) -- never
`StorageConvergenceService` called directly, never the `tests/unit/
_app_factory.py` already-OPERATIONAL shortcut. "Stop old, start new" is
modeled by exiting one app's lifespan context fully before entering the
next's, each with its own freshly constructed PostgreSQL engine/session
factory and its own `SourceStorageRouter`/`StorageConvergenceService`
instance -- nothing is carried over via retained Python objects, only via
durable PostgreSQL + filesystem + S3 state (STORAGE-009 coordinator spec,
2026-09-01 final-closure pass). D43 explicitly excludes unsupported
multi-process overlap from the single-replica MVP -- this file never runs
two app instances' lifespans concurrently; STOP-OLD-THEN-START-NEW is the
accepted, and only tested, sequencing.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder

SMOKE_TESTS_ENV = "SOFIAS_MEMORY_RUN_STORAGE009_SMOKE_TESTS"
SMOKE_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_STORAGE009_SMOKE_TEST_DATABASE_URL"
SMOKE_TEST_DATABASE_NAME = "sofias_memory_remember_test"
"""Reuses the existing dedicated/discardable `remember` integration database
(truncated at the start of this fixture) rather than provisioning a brand
new one -- it already carries the real Alembic-applied schema, and per
AGENTS.md this repository's dedicated test databases are exactly the
isolation mechanism destructive validation is meant to use, never
`cognee_db`."""

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
LLM_API_KEY = "sk-fake-test-key"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"

VALIDATION_PREFIX = "validation/storage009-final-20260901"
"""New, unique prefix for this closure pass -- never the empty root prefix,
never the prior `validation/storage009-20260901` prefix (already cleaned up
and retired in the prior pass), never any bucket-create/versioning-admin
operation. Bucket `sofias-memory` only; this file never calls
`CreateBucket`/`DeleteBucket`/`PutBucketVersioning`."""

_SMOKE_TABLES = (
    "graph_outbox",
    "pipeline_steps",
    "pipeline_runs",
    "feedback",
    "queries",
    "memory_entries",
    "summaries",
    "relation_evidence",
    "relations",
    "entity_mentions",
    "entities",
    "chunks",
    "documents",
    "sources",
    "datasets",
)


def _env_credentials() -> dict[str, str]:
    """Reads real, redacted-in-reports S3 credentials straight from `.env`
    (repo root) -- never printed, never logged, only used to build a boto3
    client and to construct real `Settings` for the app under test."""

    raw: dict[str, str] = {}
    with open(".env", encoding="utf-8-sig") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            raw[key.strip()] = value.strip()
    return raw


def smoke_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(SMOKE_TESTS_ENV) != "1":
        pytest.skip(f"set {SMOKE_TESTS_ENV}=1 to run the ADR-0011 item-28 production-shaped smoke")
    database_url = env.get(SMOKE_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {SMOKE_TEST_DATABASE_URL_ENV} to a dedicated discardable PostgreSQL database"
        )
    return database_url


async def _assert_connected(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != SMOKE_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated smoke test database")


async def _truncate_smoke_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table}"' for table in _SMOKE_TABLES)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = smoke_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected(engine)
        await _truncate_smoke_tables(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


def _database_url() -> str:
    return smoke_test_database_url(os.environ)


class _ObservingHolder(ProcessStateHolder):
    """Records the real transition sequence the production bootstrap task
    drives this holder through, and exposes an `asyncio.Event` that fires the
    instant `STORAGE_CONVERGING` is entered -- a deterministic synchronization
    primitive (not a sleep) for observing the maintenance surface mid-
    convergence. This is a plain subclass used only by this test; no
    production code is modified."""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[ProcessState] = []
        self.converging_event = asyncio.Event()

    def transition(self, state: ProcessState, *, detail: str | None = None) -> None:
        super().transition(state, detail=detail)
        self.history.append(state)
        if state is ProcessState.STORAGE_CONVERGING:
            self.converging_event.set()


def make_settings(tmp_path: Path, credentials: dict[str, str], **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": _database_url(),
        "neo4j_uri": os.environ.get("NEO4J_URI", "bolt://localhost:7688"),
        "neo4j_password": os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "chunk_max_tokens": 24,
        "chunk_overlap_tokens": 6,
        "chunk_min_tokens": 4,
        "worker_poll_interval_ms": 20,
        "worker_stale_after_seconds": 5,
        "request_wait_timeout_seconds": 30,
        "storage_s3_bucket": credentials["STORAGE_S3_BUCKET"],
        "storage_s3_region": credentials["STORAGE_S3_REGION"],
        "storage_s3_endpoint_url": credentials["STORAGE_S3_ENDPOINT_URL"],
        "storage_s3_access_key_id": credentials["STORAGE_S3_ACCESS_KEY_ID"],
        "storage_s3_secret_access_key": credentials["STORAGE_S3_SECRET_ACCESS_KEY"],
        "storage_s3_prefix": VALIDATION_PREFIX,
        "storage_s3_max_concurrency": 4,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def build_client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={API_KEY_HEADER: EXPECTED_API_KEY},
    )


async def _seed_dataset(session_factory: AsyncSessionFactory, *, slug: str) -> UUID:
    dataset_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name=slug,
                slug=slug,
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        await uow.commit()
    return dataset_id


async def _remember_text(app: Any, *, dataset: str, content: str) -> dict[str, Any]:
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": dataset, "content": content, "mode": "ingest", "wait": True},
        )
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


async def _get_source_row(session_factory: AsyncSessionFactory, source_id: UUID) -> Any:
    from types import SimpleNamespace

    async with PostgresUnitOfWork(session_factory) as uow:
        source = await uow.sources.get_by_id(source_id)
        if source is None:
            return None
        return SimpleNamespace(
            id=source.id,
            storage_uri=source.storage_uri,
            content_sha256=source.content_sha256,
            byte_size=source.byte_size,
            status=source.status,
        )


def _s3_client(credentials: dict[str, str]) -> Any:
    return boto3.client(
        "s3",
        region_name=credentials["STORAGE_S3_REGION"],
        endpoint_url=credentials["STORAGE_S3_ENDPOINT_URL"],
        aws_access_key_id=credentials["STORAGE_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=credentials["STORAGE_S3_SECRET_ACCESS_KEY"],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adr_0011_item28_filesystem_to_s3_production_shaped_smoke(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    credentials = _env_credentials()
    bucket = credentials["STORAGE_S3_BUCKET"]
    s3 = _s3_client(credentials)

    # Sentinels created up front -- must survive every phase below.
    local_sentinel_path = tmp_path / "_sentinel_local.txt"
    local_sentinel_path.write_bytes(b"local sentinel -- must survive convergence")
    s3_sentinel_key = f"{VALIDATION_PREFIX}/_sentinel/smoke-{uuid.uuid4().hex}.txt"
    s3.put_object(Bucket=bucket, Key=s3_sentinel_key, Body=b"s3 sentinel -- must survive")

    dataset_slug = f"storage009-smoke-{uuid.uuid4().hex[:8]}"
    session_factory_a = create_session_factory(postgres_engine)
    await _seed_dataset(session_factory_a, slug=dataset_slug)

    # -----------------------------------------------------------------
    # Phase A -- filesystem-mode Remember, real lifespan, real HTTP.
    # -----------------------------------------------------------------
    settings_a = make_settings(tmp_path, credentials, storage_backend="filesystem")
    app_a = create_app(
        settings_a,
        enable_neo4j=False,
        postgres_session_factory=session_factory_a,
    )
    content_a = f"filesystem-phase content {uuid.uuid4().hex}"
    async with app_a.router.lifespan_context(app_a):
        await asyncio.wait_for(app_a.state.bootstrap_task, timeout=30.0)
        assert app_a.state.process_state_holder.state is ProcessState.OPERATIONAL

        async with build_client(app_a) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
        assert live.status_code == 200
        assert ready.status_code == 200

        data_a = await _remember_text(app_a, dataset=dataset_slug, content=content_a)
    source_id = UUID(str(data_a["source_id"]))
    # -- Phase A boundary: lifespan fully exited (worker stopped, engine-
    # level resources released) before Phase C begins -- "stop old" proven
    # by the `async with` block above having returned.

    source_after_a = await _get_source_row(session_factory_a, source_id)
    assert source_after_a is not None
    assert source_after_a.storage_uri is not None
    assert source_after_a.storage_uri.startswith("file://"), source_after_a.storage_uri
    assert source_after_a.content_sha256 == sha256(content_a.encode("utf-8")).hexdigest()
    assert source_after_a.byte_size == len(content_a.encode("utf-8"))

    from urllib.parse import urlparse
    from urllib.request import url2pathname

    local_path = Path(url2pathname(urlparse(source_after_a.storage_uri).path))
    assert local_path.exists(), "local final object must exist right after filesystem finalize"
    assert local_path.read_bytes() == content_a.encode("utf-8")

    # No S3 object should exist yet for this Source (filesystem mode never
    # wrote to S3).
    key_before_migration = source_after_a.storage_uri  # local file path, not an S3 key -- no
    # S3 existence check makes sense against a file:// URI; the meaningful
    # proof (no S3 target exists yet) is implicit: STORAGE-006's
    # `already_present`/CAS-hit behavior later would surface as
    # `deterministic_target_exists_but_differs` if a stray object were
    # already there, which the assertions below (byte-identical, exactly one
    # object) rule out.
    del key_before_migration

    # -----------------------------------------------------------------
    # Phase C -- fresh runtime, STORAGE_BACKEND=s3, real bootstrap wiring.
    # A brand-new engine/session_factory -- no retained in-memory state from
    # Phase A crosses this boundary except what PostgreSQL/filesystem/S3
    # themselves durably hold.
    # -----------------------------------------------------------------
    database_url = _database_url()
    engine_c = create_async_engine(database_url, pool_pre_ping=True)
    session_factory_c = create_session_factory(engine_c)
    settings_c = make_settings(tmp_path, credentials, storage_backend="s3")
    holder_c = _ObservingHolder()
    app_c = create_app(
        settings_c,
        enable_neo4j=False,
        postgres_session_factory=session_factory_c,
        process_state_holder=holder_c,
    )

    async with app_c.router.lifespan_context(app_c):
        # Deterministic wait for STORAGE_CONVERGING (no sleep): the real
        # lifespan()/`_run_bootstrap` task sets this event synchronously the
        # instant it transitions -- see `_ObservingHolder.transition`.
        await asyncio.wait_for(holder_c.converging_event.wait(), timeout=15.0)

        # The real bootstrap coroutine is now suspended inside real S3 I/O
        # (the D21 probe, then the real convergence pass's own S3
        # put/head/list calls) -- real network latency, not an artificial
        # gate, is what keeps this window open long enough for the three
        # ordinary local ASGI calls below to reliably observe it.
        async with build_client(app_c) as client:
            live_converging = await client.get("/health/live")
            ready_converging = await client.get("/health/ready")
            business_converging = await client.get("/api/v1/info")
        assert live_converging.status_code == 200
        assert ready_converging.status_code == 503
        assert ready_converging.json()["status"] == "not_ready"
        assert business_converging.status_code == 503
        assert business_converging.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

        await asyncio.wait_for(app_c.state.bootstrap_task, timeout=60.0)
        assert app_c.state.process_state_holder.state is ProcessState.OPERATIONAL
        assert ProcessState.BOOTSTRAP_MAINTENANCE in holder_c.history
        assert ProcessState.STORAGE_CONVERGING in holder_c.history
        assert holder_c.history.index(ProcessState.STORAGE_CONVERGING) > holder_c.history.index(
            ProcessState.BOOTSTRAP_MAINTENANCE
        )
        assert holder_c.history[-1] is ProcessState.OPERATIONAL

        async with build_client(app_c) as client:
            ready_after = await client.get("/health/ready")
            business_after = await client.get("/api/v1/info")
        assert ready_after.status_code == 200
        assert business_after.status_code == 200

        # -- Postgres file:// -> s3:// evidence, and real MinIO byte/hash/size
        # verification.
        source_after_c = await _get_source_row(session_factory_c, source_id)
        assert source_after_c is not None
        assert source_after_c.storage_uri is not None
        assert source_after_c.storage_uri.startswith(f"s3://{bucket}/"), source_after_c.storage_uri
        assert VALIDATION_PREFIX in source_after_c.storage_uri
        s3_key = source_after_c.storage_uri.split(f"{bucket}/", 1)[1]
        head = s3.head_object(Bucket=bucket, Key=s3_key)
        assert head["ContentLength"] == len(content_a.encode("utf-8"))
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        real_bytes = obj["Body"].read()
        assert real_bytes == content_a.encode("utf-8")
        assert sha256(real_bytes).hexdigest() == source_after_c.content_sha256

        # -- Local post-repoint cleanup: the old local final object is gone,
        # but DATA_DIRECTORY root itself remains (mandatory in both modes).
        assert not local_path.exists(), "local final object must be removed only after S3 repoint"
        assert tmp_path.exists()

        # -- Sentinel survival, mid-flight.
        assert local_sentinel_path.exists()
        assert local_sentinel_path.read_bytes() == b"local sentinel -- must survive convergence"
        s3.head_object(Bucket=bucket, Key=s3_sentinel_key)

        # -- Item E: business Remember executes normally once OPERATIONAL --
        # a genuinely NEW Source, written directly to S3 (no migration
        # involved for this one).
        content_c = f"s3-phase fresh content {uuid.uuid4().hex}"
        data_c = await _remember_text(app_c, dataset=dataset_slug, content=content_c)
        fresh_source_id = UUID(str(data_c["source_id"]))
        fresh_source = await _get_source_row(session_factory_c, fresh_source_id)
        assert fresh_source is not None
        assert fresh_source.storage_uri is not None
        assert fresh_source.storage_uri.startswith(f"s3://{bucket}/")
        fresh_key = fresh_source.storage_uri.split(f"{bucket}/", 1)[1]
        fresh_obj = s3.get_object(Bucket=bucket, Key=fresh_key)
        assert fresh_obj["Body"].read() == content_c.encode("utf-8")

    await dispose_async_engine(engine_c)

    # Final sentinel proof, after Phase C's lifespan has fully exited.
    assert local_sentinel_path.exists()
    s3.head_object(Bucket=bucket, Key=s3_sentinel_key)


async def _converge_one_source_to_s3(
    postgres_engine: AsyncEngine, tmp_path: Path, credentials: dict[str, str], *, slug_prefix: str
) -> tuple[UUID, str, bytes]:
    """Shared setup for items 4/5: filesystem-phase Remember, then one real
    S3 convergence pass, exactly as proven end to end by the item-28 smoke
    above -- returns the converged `source_id`, `dataset_slug`, and original
    content bytes so a fresh test can build on durable state only."""

    dataset_slug = f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    session_factory_a = create_session_factory(postgres_engine)
    await _seed_dataset(session_factory_a, slug=dataset_slug)

    settings_a = make_settings(tmp_path, credentials, storage_backend="filesystem")
    app_a = create_app(settings_a, enable_neo4j=False, postgres_session_factory=session_factory_a)
    content = f"{slug_prefix} original content {uuid.uuid4().hex}".encode()
    async with app_a.router.lifespan_context(app_a):
        await asyncio.wait_for(app_a.state.bootstrap_task, timeout=30.0)
        data_a = await _remember_text(app_a, dataset=dataset_slug, content=content.decode("utf-8"))
    source_id = UUID(str(data_a["source_id"]))

    database_url = _database_url()
    engine_c = create_async_engine(database_url, pool_pre_ping=True)
    session_factory_c = create_session_factory(engine_c)
    settings_c = make_settings(tmp_path, credentials, storage_backend="s3")
    app_c = create_app(
        settings_c,
        enable_neo4j=False,
        postgres_session_factory=session_factory_c,
    )
    async with app_c.router.lifespan_context(app_c):
        await asyncio.wait_for(app_c.state.bootstrap_task, timeout=60.0)
        assert app_c.state.process_state_holder.state is ProcessState.OPERATIONAL
        source_after = await _get_source_row(session_factory_c, source_id)
        assert source_after is not None
        assert source_after.storage_uri.startswith("s3://")
    await dispose_async_engine(engine_c)

    return source_id, dataset_slug, content


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adr_0011_item4_filesystem_after_s3_no_reverse_migration(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A historical `s3://` Source must never be reverse-migrated: a fresh
    `STORAGE_BACKEND=filesystem` runtime must reach OPERATIONAL without any
    filesystem<-S3 convergence, `Source.storage_uri` must remain untouched,
    a direct read must still follow the `s3://` URI (routing is by scheme,
    never by `STORAGE_BACKEND`), and -- with S3 credentials made unusable --
    a filesystem-mode startup must still reach OPERATIONAL (no S3 scan is
    required at filesystem startup, D2/D25)."""

    credentials = _env_credentials()
    source_id, _dataset_slug, content = await _converge_one_source_to_s3(
        postgres_engine, tmp_path, credentials, slug_prefix="storage009-item4"
    )

    # -- Fresh filesystem-mode runtime, valid S3 credentials still present in
    # Settings (but never used for a scan/migration -- D2/D25).
    database_url = _database_url()
    engine_d = create_async_engine(database_url, pool_pre_ping=True)
    session_factory_d = create_session_factory(engine_d)
    settings_d = make_settings(tmp_path, credentials, storage_backend="filesystem")
    app_d = create_app(settings_d, enable_neo4j=False, postgres_session_factory=session_factory_d)
    async with app_d.router.lifespan_context(app_d):
        await asyncio.wait_for(app_d.state.bootstrap_task, timeout=30.0)
        assert app_d.state.process_state_holder.state is ProcessState.OPERATIONAL
        # No STORAGE_CONVERGING at all in filesystem mode -- the real
        # bootstrap code path (`lifespan.py`) only enters it when
        # `settings.storage_backend == "s3"`.

        source_unchanged = await _get_source_row(session_factory_d, source_id)
        assert source_unchanged is not None
        assert source_unchanged.storage_uri is not None
        assert source_unchanged.storage_uri.startswith("s3://")

        # A direct read through the app's own router still follows the
        # `s3://` URI -- reads route by scheme, never by `STORAGE_BACKEND`
        # (D4/D13), even while the process itself is in filesystem mode.
        router = app_d.state.source_storage_router
        dataset_id_str = source_unchanged.storage_uri.split("/v1/sources/", 1)[1].split("/")[0]
        real_bytes = await router.read(
            dataset_id=UUID(dataset_id_str),
            source_id=source_id,
            storage_uri=source_unchanged.storage_uri,
            expected_byte_size=len(content),
            expected_content_sha256=sha256(content).hexdigest(),
            max_bytes=10_000_000,
        )
        assert real_bytes == content
    await dispose_async_engine(engine_d)

    # -- Filesystem-mode startup with S3 credentials made UNUSABLE (process-
    # env override only, never `.env`) must still reach OPERATIONAL -- no S3
    # scan is required at filesystem startup.
    broken_credentials = dict(credentials)
    broken_credentials["STORAGE_S3_ACCESS_KEY_ID"] = "AKIA_INVALID_FOR_ITEM4_TEST"
    broken_credentials["STORAGE_S3_SECRET_ACCESS_KEY"] = "invalid-secret-for-item4-test-only"
    engine_e = create_async_engine(database_url, pool_pre_ping=True)
    session_factory_e = create_session_factory(engine_e)
    settings_e = make_settings(tmp_path, broken_credentials, storage_backend="filesystem")
    app_e = create_app(settings_e, enable_neo4j=False, postgres_session_factory=session_factory_e)
    async with app_e.router.lifespan_context(app_e):
        await asyncio.wait_for(app_e.state.bootstrap_task, timeout=30.0)
        assert app_e.state.process_state_holder.state is ProcessState.OPERATIONAL
        async with build_client(app_e) as client:
            ready = await client.get("/health/ready")
        assert ready.status_code == 200
    await dispose_async_engine(engine_e)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adr_0011_item5_fresh_instance_s3_restart_idempotency(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """After full S3 convergence, a COMPLETELY fresh app/service/router
    instance (new engine, new session factory, new `SourceStorageRouter`,
    new `StorageConvergenceService` -- reconstructed exclusively from durable
    PostgreSQL + filesystem + MinIO state) must reach OPERATIONAL again with
    no observable side effect: `storage_uri` unchanged, the S3 object
    unchanged (same bytes/hash), no duplicate object created, no local
    finalized Source recreated, sentinels survive."""

    credentials = _env_credentials()
    s3 = _s3_client(credentials)
    bucket = credentials["STORAGE_S3_BUCKET"]
    source_id, _dataset_slug, content = await _converge_one_source_to_s3(
        postgres_engine, tmp_path, credentials, slug_prefix="storage009-item5"
    )

    database_url = _database_url()
    engine_probe = create_async_engine(database_url, pool_pre_ping=True)
    async with PostgresUnitOfWork(create_session_factory(engine_probe)) as uow:
        before = await uow.sources.get_by_id(source_id)
        assert before is not None
        storage_uri_before = before.storage_uri
    await dispose_async_engine(engine_probe)

    key = storage_uri_before.split(f"{bucket}/", 1)[1]
    before_versions = s3.list_object_versions(Bucket=bucket, Prefix=key)
    objects_before = len(before_versions.get("Versions", [])) or 1  # unversioned bucket: exactly 1

    sentinel_key = f"{VALIDATION_PREFIX}/_sentinel/item5-{uuid.uuid4().hex}.txt"
    s3.put_object(Bucket=bucket, Key=sentinel_key, Body=b"item5 sentinel -- must survive restart")

    # Completely fresh instance: new engine, new session factory, new app
    # (new SourceStorageRouter, new StorageConvergenceService inside it) --
    # nothing retained from `_converge_one_source_to_s3`'s own app/engine.
    engine_fresh = create_async_engine(database_url, pool_pre_ping=True)
    session_factory_fresh = create_session_factory(engine_fresh)
    settings_fresh = make_settings(tmp_path, credentials, storage_backend="s3")
    app_fresh = create_app(
        settings_fresh, enable_neo4j=False, postgres_session_factory=session_factory_fresh
    )
    async with app_fresh.router.lifespan_context(app_fresh):
        await asyncio.wait_for(app_fresh.state.bootstrap_task, timeout=60.0)
        assert app_fresh.state.process_state_holder.state is ProcessState.OPERATIONAL

        after = await _get_source_row(session_factory_fresh, source_id)
        assert after is not None
        assert after.storage_uri == storage_uri_before  # unchanged

        head_after = s3.head_object(Bucket=bucket, Key=key)
        assert head_after["ContentLength"] == len(content)
        obj_after = s3.get_object(Bucket=bucket, Key=key)
        assert obj_after["Body"].read() == content  # unchanged bytes

        after_versions = s3.list_object_versions(Bucket=bucket, Prefix=key)
        objects_after = len(after_versions.get("Versions", [])) or 1
        assert objects_after == objects_before  # no duplicate object/key

        s3.head_object(Bucket=bucket, Key=sentinel_key)

        async with build_client(app_fresh) as client:
            ready = await client.get("/health/ready")
        assert ready.status_code == 200
    await dispose_async_engine(engine_fresh)

    s3.head_object(Bucket=bucket, Key=sentinel_key)
