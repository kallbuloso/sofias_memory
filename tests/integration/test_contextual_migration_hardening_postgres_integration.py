"""Contextual Project Memory migration hardening on a disposable PostgreSQL
database (Contract hardening gate: migration chain, upgrade from the
pre-contextual baseline, existing-data compatibility, no contextual backfill,
contextual downgrade/upgrade roundtrip and schema constraint audit).

The pre-contextual head is the real historical one produced by this repository
(``0018``, Native Cognitive Memory); the contextual chain is
``0019 -> 0020 -> 0021 -> 0022``. Pre-contextual rows (Knowledge, Sessions,
Agents, Cognitive Memory) are seeded at ``0018``, then proven usable through
the real public API after the upgrade.

Opt-in exactly like the PostgreSQL migration gate it reuses:
``SOFIAS_MEMORY_RUN_POSTGRES_MIGRATION_GATE=1`` and
``SOFIAS_MEMORY_POSTGRES_GATE_BACKEND=local|testcontainers``. Every run uses
its own throwaway database, dropped afterwards.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.infrastructure.postgres import create_session_factory
from tests.integration.test_context_checkpoints_postgres_integration import (
    HEADERS,
    make_settings,
)
from tests.integration.test_postgres_migration_gate import (
    EXPECTED_EMBEDDING_DIMENSIONS,
    DisposableDatabaseUrl,
    alembic_config,
    assert_complete_b2_schema,
    disposable_postgres_url,
    insert_minimal_records,
    single_code_head,
    vector_literal,
)
from tests.unit._app_factory import create_app

__all__ = ["disposable_postgres_url"]

PRE_CONTEXTUAL_HEAD = "0018"
CONTEXTUAL_CHAIN = ("0019", "0020", "0021", "0022")
CONTEXTUAL_TABLES = frozenset(
    {
        "episodes",
        "episode_provenance",
        "contextual_memory_idempotency",
        "context_checkpoints",
        "context_checkpoint_statements",
        "context_checkpoint_provenance",
        "contextual_evidence_refs",
        "episode_evidence_refs",
        "context_statement_evidence_refs",
    }
)
CONTEXTUAL_SCHEMA_GUARANTEES = frozenset(
    {
        # single ACTIVE checkpoint per stream and unique revisions
        "uq_context_checkpoints_active_stream",
        "uq_context_checkpoints_scope_context_key_revision",
        "ck_context_checkpoints_revision_positive",
        # scope and context_key grammar
        "ck_episodes_scope_grammar",
        "ck_episodes_context_key_grammar",
        "ck_context_checkpoints_scope_grammar",
        "ck_context_checkpoints_context_key_grammar",
        # Evidence canonical uniqueness and link uniqueness
        "uq_contextual_evidence_refs_source",
        "pk_episode_evidence_refs",
        "pk_context_statement_evidence_refs",
        # lifecycle constraints
        "ck_episodes_active_requires_clean_markers",
        "ck_episodes_invalidated_requires_markers",
        "ck_episodes_forgotten_requires_tombstone_shape",
        "ck_episodes_forgotten_has_no_support",
        "ck_context_checkpoints_active_requires_clean_markers",
        "ck_context_checkpoints_superseded_requires_markers",
        "ck_context_checkpoints_invalidated_requires_markers",
        "ck_context_checkpoints_forgotten_requires_tombstone_shape",
        # lineage constraints
        "ck_context_checkpoints_no_self_supersession",
        "ck_context_checkpoints_supersession_markers_paired",
        "fk_context_checkpoints_superseded_by_context_checkpoints",
        "uq_context_checkpoints_superseded_by",
        # generic, resource-agnostic idempotency ledger
        "uq_contextual_memory_idempotency_idempotency_key",
        "ck_contextual_memory_idempotency_request_digest_hex",
    }
)


async def _scalar(url: DisposableDatabaseUrl, sql: str, **params: Any) -> Any:
    engine = create_async_engine(url.get_secret_value())
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


async def seed_pre_contextual(url: DisposableDatabaseUrl) -> dict[str, UUID]:
    ids: dict[str, UUID] = {
        name: uuid4()
        for name in (
            "dataset_id",
            "source_id",
            "document_id",
            "chunk_a_id",
            "chunk_b_id",
            "entity_id",
            "mention_id",
            "relation_id",
            "summary_id",
            "memory_entry_id",
            "query_id",
            "feedback_id",
            "pipeline_run_id",
            "pipeline_step_id",
            "session_id",
            "entry_id",
            "agent_id",
            "memory_id",
        )
    }
    engine = create_async_engine(url.get_secret_value())
    try:
        async with engine.begin() as connection:
            await insert_minimal_records(
                connection,
                ids=ids,
                token=uuid4().hex,
                vector_a=vector_literal(EXPECTED_EMBEDDING_DIMENSIONS, hot_index=0),
                vector_b=vector_literal(EXPECTED_EMBEDDING_DIMENSIONS, hot_index=1),
            )
            await connection.execute(
                text("INSERT INTO sessions (id, key, name) VALUES (:id, :k, 'pre-contextual')"),
                {"id": ids["session_id"], "k": f"pre-contextual-{uuid4().hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO session_entries (id, session_id, role, content, metadata) "
                    "VALUES (:id, :s, 'user', 'mensagem antiga', '{}')"
                ),
                {"id": ids["entry_id"], "s": ids["session_id"]},
            )
            await connection.execute(
                text("INSERT INTO agents (id, name) VALUES (:id, :n)"),
                {"id": ids["agent_id"], "n": f"agente-{uuid4().hex[:12]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_items (id, memory_type, scope, content, embedding) "
                    "VALUES (:id, 'semantic', 'project:legacy', 'Fato anterior ao contexto', "
                    "CAST(:v AS vector))"
                ),
                {
                    "id": ids["memory_id"],
                    "v": vector_literal(EXPECTED_EMBEDDING_DIMENSIONS, hot_index=2),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_provenance (memory_id, origin_kind, source_system) "
                    "VALUES (:id, 'user_asserted', 'legacy-seed')"
                ),
                {"id": ids["memory_id"]},
            )
    finally:
        await engine.dispose()
    return ids


async def table_names(url: DisposableDatabaseUrl) -> frozenset[str]:
    engine = create_async_engine(url.get_secret_value())
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
                )
            )
            return frozenset(str(row[0]) for row in rows)
    finally:
        await engine.dispose()


async def schema_object_names(url: DisposableDatabaseUrl) -> frozenset[str]:
    engine = create_async_engine(url.get_secret_value())
    try:
        async with engine.connect() as connection:
            constraints = await connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_namespace n "
                    "ON n.oid = c.connamespace WHERE n.nspname = current_schema()"
                )
            )
            indexes = await connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
            )
            return frozenset(
                [str(row[0]) for row in constraints] + [str(row[0]) for row in indexes]
            )
    finally:
        await engine.dispose()


async def assert_pre_contextual_rows(url: DisposableDatabaseUrl, ids: dict[str, UUID]) -> None:
    for table, key in (
        ("datasets", "dataset_id"),
        ("sources", "source_id"),
        ("documents", "document_id"),
        ("chunks", "chunk_a_id"),
        ("queries", "query_id"),
        ("pipeline_runs", "pipeline_run_id"),
        ("sessions", "session_id"),
        ("session_entries", "entry_id"),
        ("agents", "agent_id"),
        ("memory_items", "memory_id"),
    ):
        found = await _scalar(
            url,
            f"SELECT count(*) FROM {table} WHERE id = :id",  # noqa: S608 - fixed names
            id=ids[key],
        )
        assert found == 1, table


async def assert_no_contextual_backfill(url: DisposableDatabaseUrl) -> None:
    for table in sorted(CONTEXTUAL_TABLES):
        assert await _scalar(url, f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608


class _FakeEmbeddingClient:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EXPECTED_EMBEDDING_DIMENSIONS - 1) for _ in texts]


async def exercise_through_public_api(
    url: DisposableDatabaseUrl, ids: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    for module in ("episodes", "memories", "context_recall"):
        monkeypatch.setattr(
            f"sofias_memory.api.routes.{module}.OpenAIEmbeddingClient",
            lambda settings: _FakeEmbeddingClient(),
        )
    engine = create_async_engine(url.get_secret_value())
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=create_session_factory(engine),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # Pre-contextual resources remain usable through their own APIs.
            for path in (
                f"/api/v1/datasets/{ids['dataset_id']}",
                f"/api/v1/sessions/{ids['session_id']}",
                f"/api/v1/agents/{ids['agent_id']}",
                f"/api/v1/memories/{ids['memory_id']}",
            ):
                response = await client.get(path, headers=HEADERS)
                assert response.status_code == 200, (path, response.text)
            # Old Cognitive Memory is valid explicit Evidence for new context.
            created = await client.post(
                "/api/v1/episodes",
                headers={**HEADERS, "Idempotency-Key": f"migration-{uuid4().hex}"},
                json={
                    "scope": "project:legacy",
                    "kind": "decision",
                    "summary": "Decisão apoiada por memória anterior ao contexto",
                    "occurred_at": "2026-11-20T10:00:00Z",
                    "provenance": {"origin_kind": "user_asserted", "source_system": "migration"},
                    "support": {
                        "mode": "all_required",
                        "refs": [
                            {"source_type": "memory_item", "source_id": str(ids["memory_id"])}
                        ],
                    },
                },
            )
            assert created.status_code == 201, created.text
            recall = await client.post(
                "/api/v1/context/recall",
                headers=HEADERS,
                json={"scope": "project:legacy", "intent": "resume", "query": "decisão"},
            )
            assert recall.status_code == 200, recall.text
            data = recall.json()["data"]
            assert data["current_checkpoint"] is None  # nothing was fabricated
            assert [e["episode"]["episode_id"] for e in data["episodes"]] == [
                created.json()["data"]["episode_id"]
            ]
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_pre_contextual_baseline_upgrades_without_backfill_and_round_trips(
    disposable_postgres_url: DisposableDatabaseUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = disposable_postgres_url
    config = alembic_config()
    head = single_code_head(config)
    assert head == CONTEXTUAL_CHAIN[-1]  # exactly one head, the end of the chain

    command.upgrade(config, PRE_CONTEXTUAL_HEAD)
    assert asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version")) == "0018"
    assert not (CONTEXTUAL_TABLES & asyncio.run(table_names(url)))
    ids = asyncio.run(seed_pre_contextual(url))

    # Upgrade step by step through the real contextual chain.
    for revision in CONTEXTUAL_CHAIN:
        command.upgrade(config, revision)
        assert asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version")) == revision
    asyncio.run(assert_complete_b2_schema(url, expected_revision=head))
    assert asyncio.run(table_names(url)) >= CONTEXTUAL_TABLES
    asyncio.run(assert_pre_contextual_rows(url, ids))
    asyncio.run(assert_no_contextual_backfill(url))
    missing = CONTEXTUAL_SCHEMA_GUARANTEES - asyncio.run(schema_object_names(url))
    assert missing == frozenset()

    # Contextual roundtrip: downgrade through the contextual boundary and back.
    command.downgrade(config, PRE_CONTEXTUAL_HEAD)
    assert asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version")) == "0018"
    assert not (CONTEXTUAL_TABLES & asyncio.run(table_names(url)))
    asyncio.run(assert_pre_contextual_rows(url, ids))
    command.upgrade(config, "head")
    asyncio.run(assert_complete_b2_schema(url, expected_revision=head))
    asyncio.run(assert_pre_contextual_rows(url, ids))
    asyncio.run(assert_no_contextual_backfill(url))

    asyncio.run(exercise_through_public_api(url, ids, monkeypatch))
