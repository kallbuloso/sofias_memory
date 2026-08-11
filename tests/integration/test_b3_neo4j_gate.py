from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sofias_memory.config import Settings, load_settings
from sofias_memory.domain import GraphOutboxStatus
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    Neo4jReadinessChecker,
    Neo4jResource,
    create_neo4j_resource_from_settings,
    delete_all_projection,
    ensure_neo4j_schema,
)
from sofias_memory.infrastructure.neo4j.projection import ProjectionEndpointMissingError
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.ports import projection_command_from_payload
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.graph_rebuild_service import GraphRebuildService

B3_GATE_ENV = "SOFIAS_MEMORY_RUN_B3_GATE"
B3_GATE_BACKEND_ENV = "SOFIAS_MEMORY_B3_GATE_BACKEND"
B3_GATE_BACKEND_TESTCONTAINERS = "testcontainers"
B3_GATE_BACKENDS = frozenset({B3_GATE_BACKEND_TESTCONTAINERS})

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
EXPECTED_EMBEDDING_DIMENSIONS = 3072


@dataclass(frozen=True, repr=False)
class B3GateContext:
    engine: AsyncEngine
    session_factory: AsyncSessionFactory
    neo4j_resource: Neo4jResource
    projection: Neo4jProjection
    processor: GraphOutboxProcessor
    rebuild: GraphRebuildService
    alembic_head: str


@dataclass(frozen=True)
class GateIds:
    dataset_a_id: str
    dataset_b_id: str
    source_a_id: str
    source_b_id: str
    document_a_id: str
    document_b_id: str
    chunk_a0_id: str
    chunk_a1_id: str
    chunk_a_old_id: str
    chunk_b0_id: str
    entity_a_id: str
    entity_b_id: str
    entity_a_old_id: str
    entity_a_inactive_id: str
    entity_b_dataset_id: str
    relation_a_id: str
    mention_a_id: str
    direct_delete_entity_id: str
    outbox_entity_id: str
    retry_source_entity_id: str
    retry_target_entity_id: str
    retry_relation_id: str
    crash_entity_id: str
    external_sentinel_id: str

    @classmethod
    def create(cls) -> GateIds:
        return cls(*(str(uuid4()) for _ in range(len(cls.__dataclass_fields__))))


@dataclass(frozen=True)
class OutboxState:
    status: str
    attempt: int
    processed_at_is_set: bool


@dataclass(frozen=True)
class GraphSnapshot:
    entities: tuple[tuple[tuple[str, object], ...], ...]
    chunks: tuple[tuple[tuple[str, object], ...], ...]
    relates_to: tuple[tuple[tuple[str, object], ...], ...]
    mentioned_in: tuple[tuple[tuple[str, object], ...], ...]
    next_relationships: tuple[tuple[tuple[str, object], ...], ...]


def require_b3_gate_backend() -> None:
    if os.environ.get(B3_GATE_ENV) != "1":
        pytest.skip(f"set {B3_GATE_ENV}=1 to run the B3 Neo4j gate")

    backend = os.environ.get(B3_GATE_BACKEND_ENV)
    if backend not in B3_GATE_BACKENDS:
        allowed = ", ".join(sorted(B3_GATE_BACKENDS))
        pytest.fail(f"set {B3_GATE_BACKEND_ENV} to one of: {allowed}")
    if backend != B3_GATE_BACKEND_TESTCONTAINERS:
        pytest.fail(f"unsupported B3 gate backend: {backend}")


@asynccontextmanager
async def _testcontainers_b3_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[B3GateContext]:
    from testcontainers.neo4j import Neo4jContainer
    from testcontainers.postgres import PostgresContainer

    postgres_container: PostgresContainer | None = None
    neo4j_container: Neo4jContainer | None = None
    engine: AsyncEngine | None = None
    neo4j_resource: Neo4jResource | None = None
    postgres_started = False
    neo4j_started = False
    neo4j_password = "sofias-memory-b3-gate-password"
    try:
        container_error: Exception | None = None
        try:
            postgres_container = PostgresContainer(
                compose_postgres_image(),
                username="sofias_memory_b3_gate",
                password="sofias_memory_b3_gate",
                dbname="sofias_memory_b3_gate",
                driver="asyncpg",
            )
            neo4j_container = Neo4jContainer(
                compose_neo4j_image(),
                password=neo4j_password,
                username="neo4j",
            )
            postgres_container.start()
            postgres_started = True
            neo4j_container.start()
            neo4j_started = True
        except Exception as exc:  # pragma: no cover - exercised only when Docker is unavailable.
            container_error = exc

        if container_error is not None:
            fail_testcontainers_backend(container_error)

        database_url = postgres_container.get_connection_url(driver="asyncpg")
        _configure_test_settings(
            monkeypatch,
            database_url=database_url,
            neo4j_uri=neo4j_container.get_connection_url(),
            neo4j_password=neo4j_password,
        )

        config = alembic_config()
        head = single_code_head(config)
        upgrade_database_to_head(config)

        settings = load_settings()
        engine = create_async_engine_from_settings(settings)
        session_factory = create_session_factory(engine)
        neo4j_resource = create_neo4j_resource_from_settings(settings)
        projection = Neo4jProjection(neo4j_resource)
        yield B3GateContext(
            engine=engine,
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
            processor=GraphOutboxProcessor(
                session_factory=session_factory,
                projection=projection,
            ),
            rebuild=GraphRebuildService(
                session_factory=session_factory,
                neo4j_resource=neo4j_resource,
                projection=projection,
            ),
            alembic_head=head,
        )
    finally:
        if neo4j_resource is not None:
            await neo4j_resource.close()
        if engine is not None:
            await dispose_async_engine(engine)
        if neo4j_started and neo4j_container is not None:
            neo4j_container.stop()
        if postgres_started and postgres_container is not None:
            postgres_container.stop()


def fail_testcontainers_backend(exc: Exception) -> NoReturn:
    pytest.fail(b3_gate_backend_failure_message(exc), pytrace=False)


def b3_gate_backend_failure_message(exc: Exception) -> str:
    return (
        "B3 gate Testcontainers backend failed before disposable databases became ready. "
        "Docker/Testcontainers must be available when SOFIAS_MEMORY_RUN_B3_GATE=1 "
        f"(error_type={type(exc).__name__})."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b3_neo4j_projection_outbox_rebuild_and_recovery_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_b3_gate_backend()

    async with _testcontainers_b3_gate(monkeypatch) as b3_gate:
        ids = GateIds.create()

        await assert_neo4j_connectivity_and_schema_ready(b3_gate.neo4j_resource)
        assert b3_gate.alembic_head == await database_revision(b3_gate.engine)
        await insert_authoritative_state(b3_gate.engine, ids)

        await exercise_direct_projection_replay_update_and_delete(
            b3_gate.projection,
            b3_gate.neo4j_resource,
            ids,
        )
        await exercise_outbox_success_retry_and_crash_replay(b3_gate, ids)
        await exercise_dataset_rebuild_and_isolation(b3_gate, ids)
        await exercise_global_reset_rebuild_and_readiness_degradation(b3_gate, ids)


def compose_postgres_image() -> str:
    return compose_service_image("postgres")


def compose_neo4j_image() -> str:
    return compose_service_image("neo4j")


def compose_service_image(service_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service_name)}:\s*$.*?^    image:\s*(?P<image>\S+)\s*$",
        COMPOSE_FILE.read_text(encoding="utf-8"),
    )
    if match is None:
        raise AssertionError(f"compose.yaml does not define image for service {service_name}")
    return match.group("image")


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def upgrade_database_to_head(config: Config) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(alembic_command.upgrade, config, "head")
        future.result()


def single_code_head(config: Config) -> str:
    heads = frozenset(ScriptDirectory.from_config(config).get_heads())
    assert len(heads) == 1
    return next(iter(heads))


def _configure_test_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
    neo4j_uri: str,
    neo4j_password: str,
) -> None:
    settings = gate_settings(
        database_url=database_url,
        neo4j_uri=neo4j_uri,
        neo4j_password=neo4j_password,
    )
    for key, value in settings.items():
        monkeypatch.setenv(key, value)


def gate_settings(*, database_url: str, neo4j_uri: str, neo4j_password: str) -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "API_KEY": f"sf-{'A' * 32}",
        "DATABASE_URL": database_url,
        "DATABASE_POOL_SIZE": "2",
        "DATABASE_MAX_OVERFLOW": "0",
        "NEO4J_URI": neo4j_uri,
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": neo4j_password,
        "NEO4J_DATABASE": "neo4j",
        "LLM_API_KEY": "test-llm-api-key",
        "EMBEDDING_DIMENSIONS": str(EXPECTED_EMBEDDING_DIMENSIONS),
    }


async def assert_neo4j_connectivity_and_schema_ready(resource: Neo4jResource) -> None:
    result = await resource.driver.execute_query("RETURN 1 AS ok", database_=resource.database)
    assert records(result)[0]["ok"] == 1

    await ensure_neo4j_schema(resource)
    readiness = await Neo4jReadinessChecker(resource).check()
    assert readiness.ready is True


async def wait_for_neo4j_ready(
    resource: Neo4jResource,
    *,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.25,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_result = None

    while time.monotonic() < deadline:
        last_result = await Neo4jReadinessChecker(resource).check()
        if last_result.ready:
            return
        await asyncio.sleep(interval_seconds)

    last_result = await Neo4jReadinessChecker(resource).check()
    if last_result.ready:
        return
    raise AssertionError(f"Neo4j did not become ready after schema restore: {last_result!r}")


async def database_revision(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        return str(result.scalar_one())


async def insert_authoritative_state(engine: AsyncEngine, ids: GateIds) -> None:
    vector = vector_literal(EXPECTED_EMBEDDING_DIMENSIONS)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO datasets (id, name, slug, description, status, active_generation)
                VALUES
                  (:dataset_a_id, :dataset_a_name, :dataset_a_slug, NULL, 'active', 1),
                  (:dataset_b_id, :dataset_b_name, :dataset_b_slug, NULL, 'active', 1)
                """
            ),
            {
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "dataset_a_name": f"B3 Gate A {ids.dataset_a_id}",
                "dataset_b_name": f"B3 Gate B {ids.dataset_b_id}",
                "dataset_a_slug": f"b3-gate-a-{ids.dataset_a_id}",
                "dataset_b_slug": f"b3-gate-b-{ids.dataset_b_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO sources (
                    id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
                    content_sha256, normalized_sha256, byte_size, metadata, status, version
                )
                VALUES
                (
                    :source_a_id, :dataset_a_id, 'text', 'B3 gate source A', 'text/plain',
                    NULL, NULL, :source_a_hash, NULL, 42, '{}'::jsonb, 'active', 1
                ),
                (
                    :source_b_id, :dataset_b_id, 'text', 'B3 gate source B', 'text/plain',
                    NULL, NULL, :source_b_hash, NULL, 42, '{}'::jsonb, 'active', 1
                )
                """
            ),
            {
                "source_a_id": ids.source_a_id,
                "source_b_id": ids.source_b_id,
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "source_a_hash": "a" * 64,
                "source_b_hash": "b" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO documents (
                    id, dataset_id, source_id, generation, title, language, normalized_text,
                    text_sha256, token_count, metadata, is_active
                )
                VALUES
                (
                    :document_a_id, :dataset_a_id, :source_a_id, 1, 'B3 gate document A',
                    'en', 'b3 gate document a', :document_a_hash, 5, '{}'::jsonb, TRUE
                ),
                (
                    :document_b_id, :dataset_b_id, :source_b_id, 1, 'B3 gate document B',
                    'en', 'b3 gate document b', :document_b_hash, 5, '{}'::jsonb, TRUE
                )
                """
            ),
            {
                "document_a_id": ids.document_a_id,
                "document_b_id": ids.document_b_id,
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "source_a_id": ids.source_a_id,
                "source_b_id": ids.source_b_id,
                "document_a_hash": "c" * 64,
                "document_b_hash": "d" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO chunks (
                    id, dataset_id, document_id, source_id, generation, ordinal, text,
                    content_sha256, token_count, start_char, end_char, section_path, metadata,
                    embedding, lexical, is_active
                )
                VALUES
                (
                    :chunk_a0_id, :dataset_a_id, :document_a_id, :source_a_id, 1, 0,
                    'b3 gate chunk a zero', :chunk_a0_hash, 5, 0, 20,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'b3 gate chunk a zero'), TRUE
                ),
                (
                    :chunk_a1_id, :dataset_a_id, :document_a_id, :source_a_id, 1, 1,
                    'b3 gate chunk a one', :chunk_a1_hash, 5, 21, 40,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'b3 gate chunk a one'), TRUE
                ),
                (
                    :chunk_a_old_id, :dataset_a_id, :document_a_id, :source_a_id, 0, 0,
                    'b3 gate old chunk', :chunk_a_old_hash, 5, 0, 17,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'b3 gate old chunk'), TRUE
                ),
                (
                    :chunk_b0_id, :dataset_b_id, :document_b_id, :source_b_id, 1, 0,
                    'b3 gate chunk b zero', :chunk_b0_hash, 5, 0, 20,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'b3 gate chunk b zero'), TRUE
                )
                """
            ),
            {
                "chunk_a0_id": ids.chunk_a0_id,
                "chunk_a1_id": ids.chunk_a1_id,
                "chunk_a_old_id": ids.chunk_a_old_id,
                "chunk_b0_id": ids.chunk_b0_id,
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "document_a_id": ids.document_a_id,
                "document_b_id": ids.document_b_id,
                "source_a_id": ids.source_a_id,
                "source_b_id": ids.source_b_id,
                "chunk_a0_hash": "e" * 64,
                "chunk_a1_hash": "f" * 64,
                "chunk_a_old_hash": "1" * 64,
                "chunk_b0_hash": "2" * 64,
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO entities (
                    id, dataset_id, generation, canonical_key, name, entity_type,
                    description, aliases, properties, confidence, importance_weight,
                    embedding, is_active
                )
                VALUES
                (
                    :entity_a_id, :dataset_a_id, 1, :entity_a_key, 'B3 Sofia', 'person',
                    'B3 gate entity A', ARRAY['B3 Sofia']::text[], '{}'::jsonb,
                    0.9, 0.8, CAST(:vector AS vector), TRUE
                ),
                (
                    :entity_b_id, :dataset_a_id, 1, :entity_b_key, 'B3 Ada', 'person',
                    'B3 gate entity B', ARRAY['B3 Ada']::text[], '{}'::jsonb,
                    0.9, 0.7, CAST(:vector AS vector), TRUE
                ),
                (
                    :entity_a_old_id, :dataset_a_id, 0, :entity_a_old_key, 'Old B3', 'person',
                    'Old generation entity', ARRAY['Old B3']::text[], '{}'::jsonb,
                    0.9, 0.7, CAST(:vector AS vector), TRUE
                ),
                (
                    :entity_a_inactive_id, :dataset_a_id, 1, :inactive_key, 'Inactive B3',
                    'person', 'Inactive entity', ARRAY['Inactive B3']::text[], '{}'::jsonb,
                    0.9, 0.7, CAST(:vector AS vector), FALSE
                ),
                (
                    :entity_b_dataset_id, :dataset_b_id, 1, :entity_b_dataset_key,
                    'Dataset B Entity', 'topic', 'B dataset sentinel entity',
                    ARRAY['Dataset B Entity']::text[], '{}'::jsonb,
                    0.9, 0.7, CAST(:vector AS vector), TRUE
                )
                """
            ),
            {
                "entity_a_id": ids.entity_a_id,
                "entity_b_id": ids.entity_b_id,
                "entity_a_old_id": ids.entity_a_old_id,
                "entity_a_inactive_id": ids.entity_a_inactive_id,
                "entity_b_dataset_id": ids.entity_b_dataset_id,
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "entity_a_key": f"entity-a-{ids.dataset_a_id}",
                "entity_b_key": f"entity-b-{ids.dataset_a_id}",
                "entity_a_old_key": f"entity-old-{ids.dataset_a_id}",
                "inactive_key": f"entity-inactive-{ids.dataset_a_id}",
                "entity_b_dataset_key": f"entity-b-{ids.dataset_b_id}",
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, chunk_id, surface_text, start_char, end_char, confidence
                )
                VALUES (:mention_a_id, :entity_a_id, :chunk_a0_id, 'B3 Sofia', 0, 8, 0.95)
                """
            ),
            {
                "mention_a_id": ids.mention_a_id,
                "entity_a_id": ids.entity_a_id,
                "chunk_a0_id": ids.chunk_a0_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO relations (
                    id, dataset_id, generation, source_entity_id, target_entity_id,
                    predicate, description, properties, confidence, importance_weight,
                    embedding, is_active
                )
                VALUES (
                    :relation_a_id, :dataset_a_id, 1, :entity_a_id, :entity_b_id,
                    'knows', 'B3 Sofia knows B3 Ada', '{}'::jsonb, 0.8, 0.7,
                    CAST(:vector AS vector), TRUE
                )
                """
            ),
            {
                "relation_a_id": ids.relation_a_id,
                "dataset_a_id": ids.dataset_a_id,
                "entity_a_id": ids.entity_a_id,
                "entity_b_id": ids.entity_b_id,
                "vector": vector,
            },
        )


async def exercise_direct_projection_replay_update_and_delete(
    projection: Neo4jProjection,
    resource: Neo4jResource,
    ids: GateIds,
) -> None:
    commands = [
        projection_command_from_payload(entity_payload(ids.entity_a_id, ids.dataset_a_id)),
        projection_command_from_payload(entity_payload(ids.entity_b_id, ids.dataset_a_id)),
        projection_command_from_payload(chunk_payload(ids.chunk_a0_id, ids, ordinal=0)),
        projection_command_from_payload(chunk_payload(ids.chunk_a1_id, ids, ordinal=1)),
        projection_command_from_payload(relation_payload(ids)),
        projection_command_from_payload(mention_payload(ids)),
        projection_command_from_payload(next_payload(ids)),
        projection_command_from_payload(entity_payload(ids.entity_b_dataset_id, ids.dataset_b_id)),
        projection_command_from_payload(
            chunk_payload(
                ids.chunk_b0_id,
                ids,
                dataset_id=ids.dataset_b_id,
                source_id=ids.source_b_id,
                document_id=ids.document_b_id,
                ordinal=0,
            )
        ),
    ]
    for projection_command in commands:
        await projection.apply(projection_command)
    for projection_command in commands:
        await projection.apply(projection_command)
    assert await entity_count(resource, ids.dataset_a_id) == 2
    assert await chunk_count(resource, ids.dataset_a_id) == 2
    assert await relationship_count(resource, "RELATES_TO") == 1
    assert await relationship_count(resource, "MENTIONED_IN") == 1
    assert await relationship_count(resource, "NEXT") == 1

    await projection.apply(
        projection_command_from_payload(
            entity_payload(ids.entity_a_id, ids.dataset_a_id, name="B3 Sofia Updated")
        )
    )
    assert await node_property(resource, ids.entity_a_id, "name") == "B3 Sofia Updated"
    assert await node_count_by_id(resource, ids.entity_a_id) == 1

    for payload in (
        relation_payload(ids, operation="delete"),
        mention_payload(ids, operation="delete"),
        next_payload(ids, operation="delete"),
    ):
        await projection.apply(projection_command_from_payload(payload))
        await projection.apply(projection_command_from_payload(payload))

    assert await relationship_count(resource, "RELATES_TO") == 0
    assert await relationship_count(resource, "MENTIONED_IN") == 0
    assert await relationship_count(resource, "NEXT") == 0

    await projection.apply(
        projection_command_from_payload(
            entity_payload(ids.direct_delete_entity_id, ids.dataset_a_id, name="Delete Me")
        )
    )
    await projection.apply(
        projection_command_from_payload(entity_delete_payload(ids.direct_delete_entity_id, ids))
    )
    await projection.apply(
        projection_command_from_payload(entity_delete_payload(ids.direct_delete_entity_id, ids))
    )
    assert await node_count_by_id(resource, ids.direct_delete_entity_id) == 0

    for projection_command in commands:
        await projection.apply(projection_command)


async def exercise_outbox_success_retry_and_crash_replay(
    gate: B3GateContext,
    ids: GateIds,
) -> None:
    success_id = await insert_outbox_event(
        gate.engine,
        payload=entity_payload(ids.outbox_entity_id, ids.dataset_a_id, name="Outbox Entity"),
    )
    result = await gate.processor.process(success_id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.attempt == 1
    assert await outbox_state(gate.engine, success_id) == OutboxState(
        status="done",
        attempt=1,
        processed_at_is_set=True,
    )
    assert await node_count_by_id(gate.neo4j_resource, ids.outbox_entity_id) == 1

    retry_id = await insert_outbox_event(gate.engine, payload=retry_relation_payload(ids))
    with pytest.raises(ProjectionEndpointMissingError):
        await gate.processor.process(retry_id)

    assert await outbox_state(gate.engine, retry_id) == OutboxState(
        status="failed",
        attempt=1,
        processed_at_is_set=False,
    )
    await gate.projection.apply(
        projection_command_from_payload(
            entity_payload(ids.retry_source_entity_id, ids.dataset_a_id, name="Retry Source")
        )
    )
    await gate.projection.apply(
        projection_command_from_payload(
            entity_payload(ids.retry_target_entity_id, ids.dataset_a_id, name="Retry Target")
        )
    )

    retry_result = await gate.processor.process(retry_id)
    assert retry_result.status == GraphOutboxStatus.DONE
    assert retry_result.attempt == 2
    assert await outbox_state(gate.engine, retry_id) == OutboxState(
        status="done",
        attempt=2,
        processed_at_is_set=True,
    )
    assert await relation_count_by_id(gate.neo4j_resource, ids.retry_relation_id) == 1

    crash_payload = entity_payload(ids.crash_entity_id, ids.dataset_a_id, name="Crash Replay")
    crash_id = await insert_outbox_event(
        gate.engine,
        payload=crash_payload,
        status="processing",
        attempt=1,
    )
    await gate.projection.apply(projection_command_from_payload(crash_payload))
    assert await node_count_by_id(gate.neo4j_resource, ids.crash_entity_id) == 1

    await set_outbox_status(gate.engine, crash_id, status="failed")
    crash_result = await gate.processor.process(crash_id)
    assert crash_result.status == GraphOutboxStatus.DONE
    assert crash_result.attempt == 2
    assert await node_count_by_id(gate.neo4j_resource, ids.crash_entity_id) == 1


async def exercise_dataset_rebuild_and_isolation(gate: B3GateContext, ids: GateIds) -> None:
    await gate.rebuild.rebuild_dataset(ids.dataset_b_id)
    result = await gate.rebuild.rebuild_dataset(ids.dataset_a_id)

    assert result.datasets == 1
    assert result.entities == 2
    assert result.chunks == 2
    assert result.entity_mentions == 1
    assert result.relations == 1
    assert result.next_relationships == 1
    assert await entity_count(gate.neo4j_resource, ids.dataset_a_id) == 2
    assert await chunk_count(gate.neo4j_resource, ids.dataset_a_id) == 2
    assert await mentioned_in_count(gate.neo4j_resource, ids) == 1
    assert await relates_to_count(gate.neo4j_resource, ids) == 1
    assert await next_count(gate.neo4j_resource, ids) == 1
    assert await node_count_by_id(gate.neo4j_resource, ids.entity_a_old_id) == 0
    assert await node_count_by_id(gate.neo4j_resource, ids.entity_a_inactive_id) == 0
    assert await node_count_by_id(gate.neo4j_resource, ids.chunk_a_old_id) == 0
    assert await node_count_by_id(gate.neo4j_resource, ids.outbox_entity_id) == 0
    assert await node_count_by_id(gate.neo4j_resource, ids.entity_b_dataset_id) == 1
    assert await node_count_by_id(gate.neo4j_resource, ids.chunk_b0_id) == 1


async def exercise_global_reset_rebuild_and_readiness_degradation(
    gate: B3GateContext,
    ids: GateIds,
) -> None:
    await create_external_sentinel(gate.neo4j_resource, ids.external_sentinel_id)
    await gate.rebuild.rebuild_all()
    expected_snapshot = await graph_snapshot(gate.neo4j_resource)

    await delete_all_projection(gate.neo4j_resource)
    assert await all_projection_node_count(gate.neo4j_resource) == 0
    assert await sofias_relationship_count(gate.neo4j_resource) == 0
    assert await external_sentinel_count(gate.neo4j_resource, ids.external_sentinel_id) == 1
    assert (await Neo4jReadinessChecker(gate.neo4j_resource).check()).ready is True

    await delete_graph_outbox_history(gate.engine)
    await gate.rebuild.rebuild_all()
    assert await graph_snapshot(gate.neo4j_resource) == expected_snapshot
    assert await external_sentinel_count(gate.neo4j_resource, ids.external_sentinel_id) == 1

    await gate.neo4j_resource.driver.execute_query(
        "DROP INDEX entity_name_index IF EXISTS",
        database_=gate.neo4j_resource.database,
    )
    assert (await Neo4jReadinessChecker(gate.neo4j_resource).check()).ready is False
    await ensure_neo4j_schema(gate.neo4j_resource)
    await wait_for_neo4j_ready(gate.neo4j_resource)


def entity_payload(
    entity_id: str,
    dataset_id: str,
    *,
    name: str = "B3 Sofia",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "aggregate_type": "entity",
        "operation": "upsert",
        "dataset_id": dataset_id,
        "aggregate_id": entity_id,
        "identity": {"id": entity_id},
        "properties": {
            "id": entity_id,
            "dataset_id": dataset_id,
            "name": name,
            "entity_type": "person",
            "description": "B3 gate projected entity.",
            "importance_weight": 0.5,
            "generation": 1,
        },
    }


def chunk_payload(
    chunk_id: str,
    ids: GateIds,
    *,
    dataset_id: str | None = None,
    source_id: str | None = None,
    document_id: str | None = None,
    ordinal: int,
) -> dict[str, object]:
    final_dataset_id = dataset_id or ids.dataset_a_id
    return {
        "schema_version": 1,
        "aggregate_type": "chunk",
        "operation": "upsert",
        "dataset_id": final_dataset_id,
        "aggregate_id": chunk_id,
        "identity": {"id": chunk_id},
        "properties": {
            "id": chunk_id,
            "dataset_id": final_dataset_id,
            "source_id": source_id or ids.source_a_id,
            "document_id": document_id or ids.document_a_id,
            "ordinal": ordinal,
            "generation": 1,
        },
    }


def relation_payload(ids: GateIds, *, operation: str = "upsert") -> dict[str, object]:
    return relationship_payload(
        dataset_id=ids.dataset_a_id,
        relation_id=ids.relation_a_id,
        source_entity_id=ids.entity_a_id,
        target_entity_id=ids.entity_b_id,
        operation=operation,
    )


def retry_relation_payload(ids: GateIds) -> dict[str, object]:
    return relationship_payload(
        dataset_id=ids.dataset_a_id,
        relation_id=ids.retry_relation_id,
        source_entity_id=ids.retry_source_entity_id,
        target_entity_id=ids.retry_target_entity_id,
        operation="upsert",
    )


def relationship_payload(
    *,
    dataset_id: str,
    relation_id: str,
    source_entity_id: str,
    target_entity_id: str,
    operation: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "aggregate_type": "relation",
        "operation": operation,
        "dataset_id": dataset_id,
        "aggregate_id": relation_id,
        "identity": {"relation_id": relation_id},
        "endpoints": {
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
        },
    }
    if operation == "upsert":
        payload["properties"] = {
            "relation_id": relation_id,
            "predicate": "knows",
            "description": "B3 gate relation.",
            "confidence": 0.8,
            "importance_weight": 0.7,
            "generation": 1,
        }
    return payload


def mention_payload(ids: GateIds, *, operation: str = "upsert") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "aggregate_type": "entity_mention",
        "operation": operation,
        "dataset_id": ids.dataset_a_id,
        "aggregate_id": ids.mention_a_id,
        "identity": {"mention_id": ids.mention_a_id},
        "endpoints": {"entity_id": ids.entity_a_id, "chunk_id": ids.chunk_a0_id},
    }
    if operation == "upsert":
        payload["properties"] = {"mention_id": ids.mention_a_id, "confidence": 0.95}
    return payload


def next_payload(ids: GateIds, *, operation: str = "upsert") -> dict[str, object]:
    return {
        "schema_version": 1,
        "aggregate_type": "chunk_next",
        "operation": operation,
        "dataset_id": ids.dataset_a_id,
        "aggregate_id": ids.chunk_a0_id,
        "identity": {"from_chunk_id": ids.chunk_a0_id, "to_chunk_id": ids.chunk_a1_id},
        "endpoints": {"from_chunk_id": ids.chunk_a0_id, "to_chunk_id": ids.chunk_a1_id},
        "properties": {},
    }


def entity_delete_payload(entity_id: str, ids: GateIds) -> dict[str, object]:
    return {
        "schema_version": 1,
        "aggregate_type": "entity",
        "operation": "delete",
        "dataset_id": ids.dataset_a_id,
        "aggregate_id": entity_id,
        "identity": {"id": entity_id},
    }


async def insert_outbox_event(
    engine: AsyncEngine,
    *,
    payload: Mapping[str, object],
    status: str = "pending",
    attempt: int = 0,
) -> int:
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                INSERT INTO graph_outbox (
                    dataset_id, aggregate_type, aggregate_id, operation, payload, status, attempt
                )
                VALUES (
                    :dataset_id, :aggregate_type, :aggregate_id, :operation,
                    CAST(:payload AS jsonb), :status, :attempt
                )
                RETURNING id
                """
            ),
            {
                "dataset_id": str(payload["dataset_id"]),
                "aggregate_type": str(payload["aggregate_type"]),
                "aggregate_id": str(payload["aggregate_id"]),
                "operation": str(payload["operation"]),
                "payload": json.dumps(payload, sort_keys=True),
                "status": status,
                "attempt": attempt,
            },
        )
        return int(result.scalar_one())


async def outbox_state(engine: AsyncEngine, event_id: int) -> OutboxState:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT status::text AS status, attempt, processed_at IS NOT NULL AS processed
                FROM graph_outbox
                WHERE id = :event_id
                """
            ),
            {"event_id": event_id},
        )
        row = result.one()
        return OutboxState(
            status=str(row.status),
            attempt=int(row.attempt),
            processed_at_is_set=bool(row.processed),
        )


async def set_outbox_status(engine: AsyncEngine, event_id: int, *, status: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE graph_outbox
                SET status = :status, processed_at = NULL
                WHERE id = :event_id
                """
            ),
            {"event_id": event_id, "status": status},
        )


async def delete_graph_outbox_history(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM graph_outbox"))


async def create_external_sentinel(resource: Neo4jResource, sentinel_id: str) -> None:
    await resource.driver.execute_query(
        "MERGE (n:ExternalSentinel {id: $id}) SET n.name = 'B3 external sentinel'",
        {"id": sentinel_id},
        database_=resource.database,
    )


async def graph_snapshot(resource: Neo4jResource) -> GraphSnapshot:
    return GraphSnapshot(
        entities=await entity_snapshot(resource),
        chunks=await chunk_snapshot(resource),
        relates_to=await relationship_snapshot(
            resource,
            "RELATES_TO",
            (
                "relation_id",
                "predicate",
                "description",
                "confidence",
                "importance_weight",
                "generation",
            ),
        ),
        mentioned_in=await relationship_snapshot(
            resource,
            "MENTIONED_IN",
            ("mention_id", "confidence"),
        ),
        next_relationships=await relationship_snapshot(resource, "NEXT", ()),
    )


async def entity_snapshot(resource: Neo4jResource) -> tuple[tuple[tuple[str, object], ...], ...]:
    result = await resource.driver.execute_query(
        """
        MATCH (n:Entity)
        RETURN n.id AS id, n.dataset_id AS dataset_id, n.name AS name,
               n.entity_type AS entity_type, n.description AS description,
               n.importance_weight AS importance_weight, n.generation AS generation
        ORDER BY id
        """,
        database_=resource.database,
    )
    return tuple(_canonical_record(record) for record in records(result))


async def chunk_snapshot(resource: Neo4jResource) -> tuple[tuple[tuple[str, object], ...], ...]:
    result = await resource.driver.execute_query(
        """
        MATCH (n:Chunk)
        RETURN n.id AS id, n.dataset_id AS dataset_id, n.source_id AS source_id,
               n.document_id AS document_id, n.ordinal AS ordinal, n.generation AS generation
        ORDER BY id
        """,
        database_=resource.database,
    )
    return tuple(_canonical_record(record) for record in records(result))


async def relationship_snapshot(
    resource: Neo4jResource,
    relationship_type: str,
    properties: tuple[str, ...],
) -> tuple[tuple[tuple[str, object], ...], ...]:
    if relationship_type not in {"RELATES_TO", "MENTIONED_IN", "NEXT"}:
        raise AssertionError("unexpected relationship type")
    property_returns = ", ".join(f"r.{name} AS {name}" for name in properties)
    optional_comma = ", " if property_returns else ""
    result = await resource.driver.execute_query(
        f"""
        MATCH (source)-[r:{relationship_type}]->(target)
        RETURN source.id AS source_id, target.id AS target_id{optional_comma}{property_returns}
        ORDER BY source_id, target_id
        """,
        database_=resource.database,
    )
    return tuple(_canonical_record(record) for record in records(result))


def _canonical_record(record: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    return tuple(sorted(record.items()))


async def entity_count(resource: Neo4jResource, dataset_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n:Entity {dataset_id: $dataset_id}) RETURN count(n) AS count",
        {"dataset_id": dataset_id},
    )


async def chunk_count(resource: Neo4jResource, dataset_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n:Chunk {dataset_id: $dataset_id}) RETURN count(n) AS count",
        {"dataset_id": dataset_id},
    )


async def all_projection_node_count(resource: Neo4jResource) -> int:
    return await count_query(
        resource,
        "MATCH (n) WHERE n:Entity OR n:Chunk RETURN count(n) AS count",
        {},
    )


async def sofias_relationship_count(resource: Neo4jResource) -> int:
    return await count_query(
        resource,
        """
        MATCH ()-[r]->()
        WHERE type(r) IN ['RELATES_TO', 'MENTIONED_IN', 'NEXT']
        RETURN count(r) AS count
        """,
        {},
    )


async def relationship_count(resource: Neo4jResource, relationship_type: str) -> int:
    if relationship_type not in {"RELATES_TO", "MENTIONED_IN", "NEXT"}:
        raise AssertionError("unexpected relationship type")
    return await count_query(
        resource,
        f"MATCH ()-[r:{relationship_type}]->() RETURN count(r) AS count",
        {},
    )


async def node_count_by_id(resource: Neo4jResource, node_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n {id: $id}) RETURN count(n) AS count",
        {"id": node_id},
    )


async def relation_count_by_id(resource: Neo4jResource, relation_id: str) -> int:
    return await count_query(
        resource,
        "MATCH ()-[r:RELATES_TO {relation_id: $relation_id}]->() RETURN count(r) AS count",
        {"relation_id": relation_id},
    )


async def mentioned_in_count(resource: Neo4jResource, ids: GateIds) -> int:
    return await count_query(
        resource,
        """
        MATCH (:Entity {id: $entity_id})-[r:MENTIONED_IN {mention_id: $mention_id}]
          ->(:Chunk {id: $chunk_id})
        RETURN count(r) AS count
        """,
        {
            "entity_id": ids.entity_a_id,
            "chunk_id": ids.chunk_a0_id,
            "mention_id": ids.mention_a_id,
        },
    )


async def relates_to_count(resource: Neo4jResource, ids: GateIds) -> int:
    return await count_query(
        resource,
        """
        MATCH (:Entity {id: $source_entity_id})
          -[r:RELATES_TO {relation_id: $relation_id}]
          ->(:Entity {id: $target_entity_id})
        RETURN count(r) AS count
        """,
        {
            "source_entity_id": ids.entity_a_id,
            "target_entity_id": ids.entity_b_id,
            "relation_id": ids.relation_a_id,
        },
    )


async def next_count(resource: Neo4jResource, ids: GateIds) -> int:
    return await count_query(
        resource,
        """
        MATCH (:Chunk {id: $from_chunk_id})-[r:NEXT]->(:Chunk {id: $to_chunk_id})
        RETURN count(r) AS count
        """,
        {"from_chunk_id": ids.chunk_a0_id, "to_chunk_id": ids.chunk_a1_id},
    )


async def external_sentinel_count(resource: Neo4jResource, sentinel_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n:ExternalSentinel {id: $id}) RETURN count(n) AS count",
        {"id": sentinel_id},
    )


async def node_property(resource: Neo4jResource, node_id: str, property_name: str) -> object:
    if property_name not in {"name"}:
        raise AssertionError("unexpected property lookup")
    result = await resource.driver.execute_query(
        f"MATCH (n {{id: $id}}) RETURN n.{property_name} AS value",
        {"id": node_id},
        database_=resource.database,
    )
    return records(result)[0]["value"]


async def count_query(
    resource: Neo4jResource,
    query: str,
    parameters: Mapping[str, object],
) -> int:
    result = await resource.driver.execute_query(
        query,
        parameters,
        database_=resource.database,
    )
    return int(records(result)[0]["count"])


def records(result: object) -> list[Mapping[str, object]]:
    return [record.data() for record in getattr(result, "records", ())]


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join("0.0" for _ in range(dimensions)) + "]"


def assert_no_secret_leakage_in_gate_reprs() -> None:
    settings = Settings(
        API_KEY=f"sf-{'A' * 32}",
        DATABASE_URL=SecretStr("postgresql+asyncpg://user:SUPER_SECRET_POSTGRES@example.test/db"),
        NEO4J_URI="bolt://example.test:7687",
        NEO4J_PASSWORD=SecretStr("SUPER_SECRET_NEO4J"),
        LLM_API_KEY=SecretStr("SUPER_SECRET_LLM"),
    )
    rendered = repr(settings)
    assert "SUPER_SECRET_POSTGRES" not in rendered
    assert "SUPER_SECRET_NEO4J" not in rendered
    assert "SUPER_SECRET_LLM" not in rendered


def test_b3_gate_repr_does_not_leak_controlled_secrets() -> None:
    assert_no_secret_leakage_in_gate_reprs()


def test_b3_gate_testcontainers_failure_message_is_safe() -> None:
    message = b3_gate_backend_failure_message(
        RuntimeError(
            "postgresql+asyncpg://user:SUPER_SECRET_POSTGRES@example.test/db "
            "bolt://neo4j:SUPER_SECRET_NEO4J@example.test"
        )
    )

    assert "RuntimeError" in message
    assert "SUPER_SECRET_POSTGRES" not in message
    assert "SUPER_SECRET_NEO4J" not in message
    assert "postgresql+asyncpg://" not in message
    assert "bolt://neo4j:" not in message
