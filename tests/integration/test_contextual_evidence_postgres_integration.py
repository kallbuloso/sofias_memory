"""Real-PostgreSQL integration suite for SM-CTX-004 first-class Evidence and
deterministic support (ADR-0019, Contract v1 SS 11-13/58, handoff 12 SS 96-112).

Real Episode/Checkpoint/Cognitive Memory services and real source rows
(SessionEntry, MemoryItem, Episode, Source, Document, Chunk). Only the
embedding provider is faked. Contention is coordinated deterministically
(a holder connection keeps a row lock; contenders are observed waiting in
``pg_stat_activity`` before release).

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a
dedicated ``DATABASE_URL`` migrated to head (0022).
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import EvidenceKey, EvidenceSourceType, SupportMode
from sofias_memory.infrastructure.postgres.models import (
    ContextStatementEvidenceRef,
    ContextualEvidenceRef,
    ContextualMemoryIdempotency,
    Episode,
    EpisodeEvidenceRef,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.episodes import EpisodeCreateRequest, EpisodeInvalidateRequest
from sofias_memory.schemas.memories import MemoryCreateRequest, MemorySupersedeRequest
from sofias_memory.services.cognitive_memory import CognitiveMemoryService
from sofias_memory.services.context_checkpoints import ContextCheckpointService
from sofias_memory.services.contextual_evidence import EvidenceResolver, is_supported
from sofias_memory.services.episodes import EpisodeService
from tests.integration.test_context_checkpoint_supersession_postgres_integration import (
    cleanup as cleanup_checkpoints,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    BASE,
    HEADERS,
    _test_database_url,
    assert_conflict,
    build_app,
    count,
    http,
    isolation_counts,
    key,
    keyed,
    ledger_count_for,
    make_settings,
    new_scope,
    session_factory,
)
from tests.integration.test_contextual_episodes_postgres_integration import (
    FakeEmbeddingClient,
)
from tests.integration.test_contextual_episodes_postgres_integration import (
    payload as episode_payload,
)

__all__ = ["session_factory"]

DIM = 3072


# --- seeding -------------------------------------------------------------------------


@dataclass
class Seeds:
    scope: str
    session_id: UUID
    session_entry: UUID
    memory: UUID
    dataset: UUID
    source: UUID
    document: UUID
    chunk: UUID


_CREATED_EPISODES: set[UUID] = set()
"""Every Episode this suite creates (Forget scrubs scope, so cleanup cannot find
tombstones by scope; it must never touch Episodes created by other suites)."""


class TrackingEpisodeService(EpisodeService):
    async def create(self, request: EpisodeCreateRequest, **kwargs: Any) -> Any:
        if kwargs.get("episode_id") is not None:
            _CREATED_EPISODES.add(kwargs["episode_id"])
        result = await super().create(request, **kwargs)
        _CREATED_EPISODES.add(result.episode_id)
        return result


def episodes(factory: AsyncSessionFactory) -> EpisodeService:
    return TrackingEpisodeService(
        make_settings(), embedding_client=FakeEmbeddingClient(), session_factory=factory
    )  # type: ignore[arg-type]


def checkpoints(factory: AsyncSessionFactory) -> ContextCheckpointService:
    return ContextCheckpointService(make_settings(), session_factory=factory)


def memories(factory: AsyncSessionFactory) -> CognitiveMemoryService:
    return CognitiveMemoryService(
        make_settings(), embedding_client=FakeEmbeddingClient(), session_factory=factory
    )  # type: ignore[arg-type]


def ref(source_type: str, source_id: UUID) -> dict[str, str]:
    return {"source_type": source_type, "source_id": str(source_id)}


def support(mode: str, *refs: dict[str, str]) -> dict[str, Any]:
    return {"mode": mode, "refs": list(refs)}


def episode_request(scope: str, **overrides: Any) -> EpisodeCreateRequest:
    return EpisodeCreateRequest.model_validate(episode_payload(scope, **overrides))


async def create_memory(factory: AsyncSessionFactory, scope: str) -> UUID:
    result = await memories(factory).create(
        MemoryCreateRequest.model_validate(
            {
                "memory_type": "semantic",
                "scope": scope,
                "content": f"Fato cognitivo {uuid4().hex}",
                "provenance": {
                    "origin_kind": "user_asserted",
                    "source_system": "evidence-test",
                    "source_ref": "evidence-test-fixture",
                },
            }
        ),
        idempotency_key=None,
    )
    return result.memory_id


async def seed(factory: AsyncSessionFactory) -> Seeds:
    scope = new_scope()
    ids = {name: uuid4() for name in ("session", "entry", "dataset", "source", "document", "chunk")}
    vector = "[" + ",".join(["0.01"] * DIM) + "]"
    async with factory() as s:
        await s.execute(
            text("INSERT INTO sessions (id, key) VALUES (:id, :k)"),
            {"id": ids["session"], "k": f"ev-{uuid4().hex}"},
        )
        await s.execute(
            text(
                "INSERT INTO session_entries (id, session_id, role, content, metadata) "
                "VALUES (:id, :s, 'user', 'conteúdo de sessão', '{}')"
            ),
            {"id": ids["entry"], "s": ids["session"]},
        )
        await s.execute(
            text(
                "INSERT INTO datasets (id, name, slug, status, active_generation) "
                "VALUES (:id, :n, :slug, 'active', 1)"
            ),
            {"id": ids["dataset"], "n": f"ev-{uuid4().hex[:16]}", "slug": f"ev-{uuid4().hex[:16]}"},
        )
        await s.execute(
            text(
                "INSERT INTO sources (id, dataset_id, kind, name, mime_type, content_sha256, "
                "byte_size, metadata, status, version) VALUES (:id, :d, 'text', 'n', 'text/plain', "
                ":sha, 1, '{}', 'active', 1)"
            ),
            {"id": ids["source"], "d": ids["dataset"], "sha": "a" * 64},
        )
        await s.execute(
            text(
                "INSERT INTO documents (id, dataset_id, source_id, generation, title, language, "
                "normalized_text, text_sha256, token_count, metadata, is_active) VALUES "
                "(:id, :d, :src, 1, 't', 'pt', 'texto', :sha, 1, '{}', true)"
            ),
            {"id": ids["document"], "d": ids["dataset"], "src": ids["source"], "sha": "b" * 64},
        )
        await s.execute(
            text(
                "INSERT INTO chunks (id, dataset_id, document_id, source_id, generation, ordinal, "
                "text, content_sha256, token_count, start_char, end_char, section_path, metadata, "
                "embedding, lexical, is_active) VALUES (:id, :d, :doc, :src, 1, 0, 'texto', :sha, "
                "1, 0, 5, '{}', '{}', CAST(:v AS vector), to_tsvector('simple', 'texto'), true)"
            ),
            {
                "id": ids["chunk"],
                "d": ids["dataset"],
                "doc": ids["document"],
                "src": ids["source"],
                "sha": "c" * 64,
                "v": vector,
            },
        )
        await s.commit()
    memory = await create_memory(factory, scope)
    return Seeds(
        scope=scope,
        session_id=ids["session"],
        session_entry=ids["entry"],
        memory=memory,
        dataset=ids["dataset"],
        source=ids["source"],
        document=ids["document"],
        chunk=ids["chunk"],
    )


async def cleanup(
    factory: AsyncSessionFactory, seeds: Seeds, extra_scopes: Sequence[str] = ()
) -> None:
    scopes = [seeds.scope, *extra_scopes]
    await cleanup_checkpoints(factory, scopes)
    async with factory() as s:
        episode_ids = list(
            (await s.scalars(select(Episode.id).where(Episode.scope.in_(scopes)))).all()
        )
        all_eps = list({*episode_ids, *_CREATED_EPISODES})
        if all_eps:
            await s.execute(
                text(
                    "DELETE FROM contextual_memory_idempotency WHERE result_resource_id = ANY(:i) "
                    "OR target_resource_id = ANY(:i)"
                ),
                {"i": all_eps},
            )
            await s.execute(
                text("UPDATE episodes SET replacement_episode_id = NULL WHERE id = ANY(:i)"),
                {"i": all_eps},
            )
            await s.execute(text("DELETE FROM episodes WHERE id = ANY(:i)"), {"i": all_eps})
        # Only canonical identities of this suite's sources/Episodes, and only if
        # no other owner still links them.
        own_sources = [
            seeds.session_entry,
            seeds.memory,
            seeds.source,
            seeds.document,
            seeds.chunk,
            *all_eps,
        ]
        await s.execute(
            text(
                "DELETE FROM contextual_evidence_refs r WHERE r.source_id = ANY(:ids) "
                "AND NOT EXISTS (SELECT 1 FROM episode_evidence_refs l "
                "WHERE l.evidence_ref_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM context_statement_evidence_refs l "
                "WHERE l.evidence_ref_id = r.id)"
            ),
            {"ids": own_sources},
        )
        await s.execute(text("DELETE FROM chunks WHERE id = :i"), {"i": seeds.chunk})
        await s.execute(text("DELETE FROM documents WHERE dataset_id = :d"), {"d": seeds.dataset})
        await s.execute(text("DELETE FROM sources WHERE dataset_id = :d"), {"d": seeds.dataset})
        await s.execute(text("DELETE FROM datasets WHERE id = :d"), {"d": seeds.dataset})
        await s.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": seeds.session_id})
        await s.commit()


async def availability_of(
    factory: AsyncSessionFactory, source_type: EvidenceSourceType, source_id: UUID
) -> str:
    async with PostgresUnitOfWork(factory) as uow:
        resolved = await EvidenceResolver(uow).resolve([EvidenceKey(source_type, source_id)])
        return resolved[EvidenceKey(source_type, source_id)].availability.value


async def lock_waiters(probe: Any) -> int:
    value = await probe.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
        )
    )
    await probe.rollback()
    return int(value or 0)


async def run_with_row_held(table: str, row_id: UUID, *factories: Any) -> list[Any]:
    """Holds ``SELECT ... FOR UPDATE`` on one source row while contenders start
    one by one (each observed blocked), then releases."""

    engine = create_async_engine(_test_database_url(dict(os.environ)))
    try:
        async with engine.connect() as holder, engine.connect() as probe:
            await holder.execute(
                text(f"SELECT id FROM {table} WHERE id = :i FOR UPDATE"),  # noqa: S608 - test-fixed table
                {"i": row_id},
            )
            tasks = []
            for n, start in enumerate(factories, start=1):
                tasks.append(asyncio.ensure_future(start()))
                for _ in range(500):
                    if await lock_waiters(probe) >= n:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("contender never blocked on the source row")
            await holder.rollback()
        return list(await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 60))
    finally:
        await engine.dispose()


# --- six source types, canonical hydration ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_supported_by_all_six_source_types_hydrates_canonically(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    svc = episodes(session_factory)
    try:
        a = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        refs = [
            ref("session_entry", seeds.session_entry),
            ref("memory_item", seeds.memory),
            ref("episode", a.episode_id),
            ref("source", seeds.source),
            ref("document", seeds.document),
            ref("chunk", seeds.chunk),
        ]
        random.Random(4).shuffle(refs)
        b = await svc.create(
            episode_request(seeds.scope, support=support("all_required", *refs)),
            idempotency_key=key(),
        )
        assert b.support is not None and b.support.mode is SupportMode.ALL_REQUIRED
        observed = [(e.source_type.value, str(e.source_id)) for e in b.support.evidence]
        assert observed == sorted(observed)
        assert {e.availability.value for e in b.support.evidence} == {"available"}
        assert is_supported(b.support) is True
        assert len({e.evidence_id for e in b.support.evidence}) == 6
        got = await svc.get(b.episode_id)
        assert got.support == b.support
        assert (
            await count(
                session_factory, EpisodeEvidenceRef, EpisodeEvidenceRef.episode_id == b.episode_id
            )
            == 6
        )
        # Evidence never copies source content.
        async with session_factory() as s:
            columns = set(
                (
                    await s.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_name "
                            "IN ('contextual_evidence_refs','episode_evidence_refs',"
                            "'context_statement_evidence_refs')"
                        )
                    )
                ).all()
            )
        assert columns == {
            "id",
            "source_type",
            "source_id",
            "created_at",
            "episode_id",
            "evidence_ref_id",
            "statement_id",
        }
        assert a.support is None
    finally:
        await cleanup(session_factory, seeds)


# --- resolver lifecycles (read-time, no mutation) -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolver_lifecycles_for_every_source_type(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    svc = episodes(session_factory)
    av = availability_of
    T = EvidenceSourceType
    try:
        # SessionEntry: archive is not Forget; missing -> unavailable.
        async with session_factory() as s:
            await s.execute(
                text("UPDATE sessions SET status = 'archived', archived_at = now() WHERE id = :i"),
                {"i": seeds.session_id},
            )
            await s.commit()
        assert await av(session_factory, T.SESSION_ENTRY, seeds.session_entry) == "available"
        assert await av(session_factory, T.SESSION_ENTRY, uuid4()) == "unavailable"

        # MemoryItem: active/superseded available, forgotten forgotten.
        assert await av(session_factory, T.MEMORY_ITEM, seeds.memory) == "available"
        await memories(session_factory).supersede(
            seeds.memory,
            MemorySupersedeRequest.model_validate(
                {
                    "content": "novo",
                    "provenance": {
                        "origin_kind": "user_asserted",
                        "source_system": "t",
                        "source_ref": "evidence-test-fixture",
                    },
                }
            ),
            idempotency_key=None,
        )
        assert await av(session_factory, T.MEMORY_ITEM, seeds.memory) == "available"
        await memories(session_factory).forget(seeds.memory, idempotency_key=None)
        assert await av(session_factory, T.MEMORY_ITEM, seeds.memory) == "forgotten"

        # Episode: active / invalidated / forgotten.
        a = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        assert await av(session_factory, T.EPISODE, a.episode_id) == "available"
        await svc.invalidate(
            a.episode_id, EpisodeInvalidateRequest(reason="r"), idempotency_key=key()
        )
        assert await av(session_factory, T.EPISODE, a.episode_id) == "unavailable"
        await svc.forget(a.episode_id, idempotency_key=key())
        assert await av(session_factory, T.EPISODE, a.episode_id) == "forgotten"

        # Knowledge: generation/is_active eligibility, then Forget of the Source.
        for t in (T.SOURCE, T.DOCUMENT, T.CHUNK):
            assert await av(session_factory, t, getattr(seeds, t.value)) == "available"
        async with session_factory() as s:
            await s.execute(
                text("UPDATE chunks SET is_active = false WHERE id = :i"), {"i": seeds.chunk}
            )
            await s.commit()
        assert await av(session_factory, T.CHUNK, seeds.chunk) == "unavailable"
        async with session_factory() as s:
            await s.execute(
                text("UPDATE datasets SET active_generation = 2 WHERE id = :i"),
                {"i": seeds.dataset},
            )
            await s.commit()
        assert await av(session_factory, T.DOCUMENT, seeds.document) == "unavailable"
        assert await av(session_factory, T.SOURCE, seeds.source) == "available"
        for status in ("pending", "processing", "failed", "deleting"):
            async with session_factory() as s:
                await s.execute(
                    text(f"UPDATE sources SET status = '{status}' WHERE id = :i"),  # noqa: S608
                    {"i": seeds.source},
                )
                await s.commit()
            assert await av(session_factory, T.SOURCE, seeds.source) == "unavailable", status
        async with session_factory() as s:
            await s.execute(
                text("UPDATE sources SET status = 'deleted' WHERE id = :i"), {"i": seeds.source}
            )
            await s.commit()
        for t in (T.SOURCE, T.DOCUMENT, T.CHUNK):
            assert await av(session_factory, t, getattr(seeds, t.value)) == "forgotten", t
        for t in (T.MEMORY_ITEM, T.EPISODE, T.SOURCE, T.DOCUMENT, T.CHUNK):
            assert await av(session_factory, t, uuid4()) == "unavailable"
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_and_chunk_follow_authoritative_generation(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    T = EvidenceSourceType
    try:
        async with session_factory() as s:
            await s.execute(
                text("UPDATE documents SET is_active = false WHERE id = :i"), {"i": seeds.document}
            )
            await s.commit()
        assert await availability_of(session_factory, T.DOCUMENT, seeds.document) == "unavailable"
        assert await availability_of(session_factory, T.CHUNK, seeds.chunk) == "unavailable"
        async with session_factory() as s:
            await s.execute(
                text("UPDATE documents SET is_active = true WHERE id = :i"), {"i": seeds.document}
            )
            await s.execute(
                text("UPDATE datasets SET status = 'deleting' WHERE id = :i"), {"i": seeds.dataset}
            )
            await s.commit()
        assert await availability_of(session_factory, T.CHUNK, seeds.chunk) == "unavailable"
        async with session_factory() as s:
            await s.execute(
                text("UPDATE datasets SET status = 'active' WHERE id = :i"), {"i": seeds.dataset}
            )
            await s.commit()
        assert await availability_of(session_factory, T.CHUNK, seeds.chunk) == "available"
    finally:
        await cleanup(session_factory, seeds)


# --- creation rules / project boundary / atomicity ------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_rejects_missing_unavailable_forgotten_and_cross_project_atomically(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    other = new_scope()
    svc = episodes(session_factory)
    try:
        good = ref("session_entry", seeds.session_entry)
        invalidated = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        await svc.invalidate(
            invalidated.episode_id, EpisodeInvalidateRequest(reason="r"), idempotency_key=key()
        )
        forgotten = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        await svc.forget(forgotten.episode_id, idempotency_key=key())
        foreign_episode = await svc.create(episode_request(other), idempotency_key=key())
        foreign_memory = await create_memory(session_factory, other)
        global_memory = await create_memory(session_factory, "global")
        before = await count(session_factory, Episode, Episode.scope == seeds.scope)
        cases = [
            (support("all_required", ref("memory_item", uuid4())), "EVIDENCE_NOT_FOUND"),
            (support("all_required", good, ref("session_entry", uuid4())), "EVIDENCE_NOT_FOUND"),
            (
                support("any_sufficient", good, ref("episode", invalidated.episode_id)),
                "EVIDENCE_UNAVAILABLE",
            ),
            (
                support("any_sufficient", good, ref("episode", forgotten.episode_id)),
                "EVIDENCE_UNAVAILABLE",
            ),
            (
                support("all_required", ref("episode", foreign_episode.episode_id)),
                "INVALID_EVIDENCE_REFERENCE",
            ),
            (
                support("all_required", ref("memory_item", foreign_memory)),
                "INVALID_EVIDENCE_REFERENCE",
            ),
        ]
        for declared, code in cases:
            idem = key("bad")
            with pytest.raises(SofiasMemoryError) as caught:
                await svc.create(
                    episode_request(seeds.scope, support=declared), idempotency_key=idem
                )
            assert_conflict(caught.value, code)
            assert (
                await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem))
                == 0
            )
        assert await count(session_factory, Episode, Episode.scope == seeds.scope) == before
        ok = await svc.create(
            episode_request(
                seeds.scope, support=support("all_required", ref("memory_item", global_memory))
            ),
            idempotency_key=key(),
        )
        assert ok.support is not None and ok.support.evidence[0].availability.value == "available"

        # Checkpoint Create and Supersede enforce the same boundary atomically.
        cps = checkpoints(session_factory)
        body = {
            "scope": seeds.scope,
            "context_key": "feature:evidence",
            "current_state": {
                "text": "estado",
                "support": support("all_required", ref("episode", foreign_episode.episode_id)),
            },
            "provenance": {"origin_kind": "user_asserted", "source_system": "t"},
        }
        with pytest.raises(SofiasMemoryError) as caught:
            await cps.create_current(
                ContextCheckpointCreateRequest.model_validate(body), idempotency_key=key()
            )
        assert_conflict(caught.value, "INVALID_EVIDENCE_REFERENCE")
        body["current_state"] = {"text": "estado"}
        created = await cps.create_current(
            ContextCheckpointCreateRequest.model_validate(body), idempotency_key=key()
        )
        body["current_state"] = {
            "text": "novo",
            "support": support("all_required", ref("memory_item", foreign_memory)),
        }
        idem = key("sup-bad")
        with pytest.raises(SofiasMemoryError) as caught:
            await cps.supersede(
                created.checkpoint_id,
                ContextCheckpointSupersedeRequest.model_validate(body),
                idempotency_key=idem,
            )
        assert_conflict(caught.value, "INVALID_EVIDENCE_REFERENCE")
        assert (await cps.get(created.checkpoint_id)).lifecycle.value == "active"
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 0
        )
    finally:
        await cleanup(session_factory, seeds, [other])


# --- chains / cycles / provenance isolation ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_episode_chain_and_cycle_rejection_without_partial_writes(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    svc = episodes(session_factory)
    try:
        a = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        b = await svc.create(
            episode_request(
                seeds.scope, support=support("all_required", ref("episode", a.episode_id))
            ),
            idempotency_key=key(),
        )
        c = await svc.create(
            episode_request(
                seeds.scope, support=support("all_required", ref("episode", b.episode_id))
            ),
            idempotency_key=key(),
        )
        assert c.support is not None and c.support.evidence[0].source_id == b.episode_id

        # Self-reference through the internal id hook.
        self_id = uuid4()
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.create(
                episode_request(
                    seeds.scope, support=support("all_required", ref("episode", self_id))
                ),
                idempotency_key=key(),
                episode_id=self_id,
            )
        assert_conflict(caught.value, "INVALID_EVIDENCE_CYCLE")

        # Indirect cycle X -> C -> B -> A -> X (A -> X planted structurally).
        x = uuid4()
        async with session_factory() as s:
            ref_id = uuid4()
            await s.execute(
                text(
                    "INSERT INTO contextual_evidence_refs (id, source_type, source_id) "
                    "VALUES (:id, 'episode', :x)"
                ),
                {"id": ref_id, "x": x},
            )
            await s.execute(
                text(
                    "INSERT INTO episode_evidence_refs (episode_id, evidence_ref_id) "
                    "VALUES (:a, :r)"
                ),
                {"a": a.episode_id, "r": ref_id},
            )
            await s.commit()
        idem = key("cycle")
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.create(
                episode_request(
                    seeds.scope, support=support("all_required", ref("episode", c.episode_id))
                ),
                idempotency_key=idem,
                episode_id=x,
            )
        assert_conflict(caught.value, "INVALID_EVIDENCE_CYCLE")
        assert await count(session_factory, Episode, Episode.id == x) == 0
        assert (
            await count(session_factory, ContextualMemoryIdempotency, ledger_count_for(idem)) == 0
        )
        async with session_factory() as s:
            await s.execute(
                text(
                    "DELETE FROM episode_evidence_refs WHERE episode_id = :a "
                    "AND evidence_ref_id = :r"
                ),
                {"a": a.episode_id, "r": ref_id},
            )
            await s.commit()
    finally:
        await cleanup(session_factory, seeds)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_never_creates_evidence(session_factory: AsyncSessionFactory) -> None:
    seeds = await seed(session_factory)
    try:
        e = await episodes(session_factory).create(
            episode_request(
                seeds.scope,
                provenance={
                    "origin_kind": "user_asserted",
                    "source_system": "sofias-personal-assistant",
                    "conversation_ref": str(seeds.session_entry),
                    "turn_ref": str(seeds.memory),
                },
            ),
            idempotency_key=key(),
        )
        assert e.support is None
        assert (
            await count(
                session_factory, EpisodeEvidenceRef, EpisodeEvidenceRef.episode_id == e.episode_id
            )
            == 0
        )
        async with session_factory() as s:
            mode = await s.scalar(select(Episode.support_mode).where(Episode.id == e.episode_id))
        assert mode is None
    finally:
        await cleanup(session_factory, seeds)


# --- statement-level support / supersede / checkpoint forget ----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_statement_level_support_supersede_replacement_and_forget(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    eps = episodes(session_factory)
    cps = checkpoints(session_factory)
    try:
        a = await eps.create(episode_request(seeds.scope), idempotency_key=key())
        b = await eps.create(episode_request(seeds.scope), idempotency_key=key())
        body: dict[str, Any] = {
            "scope": seeds.scope,
            "context_key": "feature:evidence",
            "objective": {"text": "objetivo sem suporte"},
            "current_state": {
                "text": "estado",
                "support": support("all_required", ref("episode", a.episode_id)),
            },
            "next_action": {
                "text": "ação",
                "support": support("any_sufficient", ref("session_entry", seeds.session_entry)),
            },
            "provenance": {"origin_kind": "user_asserted", "source_system": "t"},
        }
        rev1 = await cps.create_current(
            ContextCheckpointCreateRequest.model_validate(body), idempotency_key=key()
        )
        assert rev1.objective is not None and rev1.objective.support is None
        assert rev1.current_state is not None and rev1.current_state.support is not None
        assert rev1.current_state.support.evidence[0].source_id == a.episode_id
        assert rev1.next_action is not None and rev1.next_action.support is not None
        assert rev1.next_action.support.mode is SupportMode.ANY_SUFFICIENT
        assert rev1.next_action.support.evidence[0].source_type.value == "session_entry"
        assert await cps.get(rev1.checkpoint_id) == rev1

        body2 = {
            **body,
            "current_state": {
                "text": "estado 2",
                "support": support("all_required", ref("episode", b.episode_id)),
            },
            "next_action": {"text": "ação 2"},
        }
        rev2 = await cps.supersede(
            rev1.checkpoint_id,
            ContextCheckpointSupersedeRequest.model_validate(body2),
            idempotency_key=key(),
        )
        assert rev2.current_state is not None and rev2.current_state.support is not None
        assert rev2.current_state.support.evidence[0].source_id == b.episode_id
        assert rev2.next_action is not None and rev2.next_action.support is None  # never inherited
        old = await cps.get(rev1.checkpoint_id)
        assert old.current_state is not None and old.current_state.support is not None
        assert old.current_state.support.evidence[0].source_id == a.episode_id  # history keeps A

        statement_ids = [rev1.current_state.statement_id, rev1.next_action.statement_id]
        await cps.forget(rev1.checkpoint_id, idempotency_key=key())
        assert (
            await count(
                session_factory,
                ContextStatementEvidenceRef,
                ContextStatementEvidenceRef.statement_id.in_(statement_ids),
            )
            == 0
        )
        # The canonical identity of A survives Forget of the checkpoint (shared row).
        assert (
            await count(
                session_factory,
                ContextualEvidenceRef,
                ContextualEvidenceRef.source_type == "episode",
                ContextualEvidenceRef.source_id == a.episode_id,
            )
            == 1
        )
        assert (await cps.get(rev2.checkpoint_id)).current_state == rev2.current_state
    finally:
        await cleanup(session_factory, seeds)


# --- canonical race -----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_creates_converge_on_one_canonical_evidence_row(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    try:
        fresh_memory = await create_memory(session_factory, seeds.scope)
        declared = support("all_required", ref("memory_item", fresh_memory))
        results = await run_with_row_held(
            "memory_items",
            fresh_memory,
            lambda: episodes(session_factory).create(
                episode_request(seeds.scope, support=declared), idempotency_key=key()
            ),
            lambda: checkpoints(session_factory).create_current(
                ContextCheckpointCreateRequest.model_validate(
                    {
                        "scope": seeds.scope,
                        "context_key": "feature:race",
                        "current_state": {"text": "s", "support": declared},
                        "provenance": {"origin_kind": "user_asserted", "source_system": "t"},
                    }
                ),
                idempotency_key=key(),
            ),
        )
        assert all(not isinstance(r, BaseException) for r in results), results
        assert (
            await count(
                session_factory,
                ContextualEvidenceRef,
                ContextualEvidenceRef.source_type == "memory_item",
                ContextualEvidenceRef.source_id == fresh_memory,
            )
            == 1
        )
        ids = {
            results[0].support.evidence[0].evidence_id,
            results[1].current_state.support.evidence[0].evidence_id,
        }
        assert len(ids) == 1
    finally:
        await cleanup(session_factory, seeds)


# --- serialization with source lifecycle changes -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["create", "forget"])
async def test_derived_create_serializes_with_concurrent_source_forget(
    session_factory: AsyncSessionFactory, first: str
) -> None:
    seeds = await seed(session_factory)
    try:
        a = await episodes(session_factory).create(
            episode_request(seeds.scope), idempotency_key=key()
        )

        def create() -> Any:
            return episodes(session_factory).create(
                episode_request(
                    seeds.scope, support=support("all_required", ref("episode", a.episode_id))
                ),
                idempotency_key=key(),
            )

        def forget() -> Any:
            return episodes(session_factory).forget(a.episode_id, idempotency_key=key())

        order = [create, forget] if first == "create" else [forget, create]
        results = await run_with_row_held("episodes", a.episode_id, *order)
        outcome = dict(zip([f.__name__ for f in order], results, strict=True))
        assert not isinstance(outcome["forget"], BaseException), outcome["forget"]
        if first == "create":
            derived = outcome["create"]
            assert not isinstance(derived, BaseException), derived
            refreshed = await episodes(session_factory).get(derived.episode_id)
            # SM-CTX-005: the dependent committed first is converged by the
            # Forget's propagation (it waited on the row, then saw the link).
            assert refreshed.lifecycle.value == "forgotten"
            assert refreshed.support is None and refreshed.summary is None
        else:
            assert_conflict(outcome["create"], "EVIDENCE_UNAVAILABLE")
    finally:
        await cleanup(session_factory, seeds)


# --- idempotency / replay ----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_precedes_revalidation_and_order_is_not_identity(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    svc = episodes(session_factory)
    try:
        a = await svc.create(episode_request(seeds.scope), idempotency_key=key())
        base = episode_payload(seeds.scope)
        forward = {
            **base,
            "support": support(
                "all_required",
                ref("episode", a.episode_id),
                ref("session_entry", seeds.session_entry),
            ),
        }
        backward = {
            **base,
            "support": support(
                "all_required",
                ref("session_entry", seeds.session_entry),
                ref("episode", a.episode_id),
            ),
        }
        idem = key("order")
        first = await svc.create(EpisodeCreateRequest.model_validate(forward), idempotency_key=idem)
        same = await svc.create(EpisodeCreateRequest.model_validate(backward), idempotency_key=idem)
        assert same.episode_id == first.episode_id

        await svc.invalidate(
            a.episode_id, EpisodeInvalidateRequest(reason="r"), idempotency_key=key()
        )
        replay = await svc.create(
            EpisodeCreateRequest.model_validate(forward), idempotency_key=idem
        )
        assert replay.episode_id == first.episode_id
        assert replay.support is not None
        assert {e.source_type.value: e.availability.value for e in replay.support.evidence}[
            "episode"
        ] == "unavailable"
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.create(EpisodeCreateRequest.model_validate(forward), idempotency_key=key())
        assert_conflict(caught.value, "EVIDENCE_UNAVAILABLE")
        with pytest.raises(SofiasMemoryError) as caught:
            await svc.create(
                EpisodeCreateRequest.model_validate(
                    {**forward, "support": support("any_sufficient", ref("episode", a.episode_id))}
                ),
                idempotency_key=idem,
            )
        assert_conflict(caught.value, "IDEMPOTENCY_CONFLICT")
    finally:
        await cleanup(session_factory, seeds)


# --- forget without propagation / read-time only -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_forget_scrubs_own_support_and_reads_never_mutate(
    session_factory: AsyncSessionFactory,
) -> None:
    seeds = await seed(session_factory)
    svc = episodes(session_factory)
    try:
        before = await isolation_counts(session_factory)
        a = await svc.create(
            episode_request(
                seeds.scope,
                support=support("all_required", ref("session_entry", seeds.session_entry)),
            ),
            idempotency_key=key(),
        )
        b = await svc.create(
            episode_request(
                seeds.scope, support=support("all_required", ref("episode", a.episode_id))
            ),
            idempotency_key=key(),
        )
        c = await svc.create(
            episode_request(
                seeds.scope,
                support=support(
                    "any_sufficient", ref("episode", a.episode_id), ref("memory_item", seeds.memory)
                ),
            ),
            idempotency_key=key(),
        )
        tomb = await svc.forget(a.episode_id, idempotency_key=key())
        assert tomb.support is None
        assert (
            await count(
                session_factory, EpisodeEvidenceRef, EpisodeEvidenceRef.episode_id == a.episode_id
            )
            == 0
        )
        # SM-CTX-005: B (all_required [A]) lost support and was converged by the
        # Forget itself; C (any_sufficient [A, M]) stays. Reads never mutate.
        for _ in range(2):
            got_b = await svc.get(b.episode_id)
            got_c = await svc.get(c.episode_id)
            assert got_b.lifecycle.value == "forgotten" and got_b.support is None
            assert got_c.lifecycle.value == "active"
            assert is_supported(got_c.support) is True
        await memories(session_factory).forget(seeds.memory, idempotency_key=None)
        got_c = await svc.get(c.episode_id)
        assert got_c.lifecycle.value == "forgotten" and got_c.support is None
        after = await isolation_counts(session_factory)
        for name in (
            "GraphOutbox",
            "PipelineRun",
            "PipelineStep",
            "Session",
            "SessionEntry",
            "Dataset",
        ):
            assert after[name] == before[name], name
    finally:
        await cleanup(session_factory, seeds)


# --- HTTP surface --------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_episode_and_checkpoint_support_and_validation_codes(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = await seed(session_factory)
    monkeypatch.setattr(
        "sofias_memory.api.routes.episodes.OpenAIEmbeddingClient",
        lambda settings: FakeEmbeddingClient(),
    )
    try:
        async with http(build_app(session_factory)) as client:
            body = {
                **episode_payload(seeds.scope),
                "support": support("all_required", ref("session_entry", seeds.session_entry)),
            }
            created = await client.post("/api/v1/episodes", json=body, headers=keyed())
            assert created.status_code == 201, created.text
            evidence = created.json()["data"]["support"]["evidence"][0]
            assert set(evidence) == {"evidence_id", "source_type", "source_id", "availability"}
            listed = await client.get(
                "/api/v1/episodes", params={"scope": seeds.scope}, headers=HEADERS
            )
            assert listed.json()["data"]["items"][0]["support"]["mode"] == "all_required"
            plain = await client.post(
                "/api/v1/episodes", json=episode_payload(seeds.scope), headers=keyed()
            )
            assert plain.json()["data"]["support"] is None
            for bad, code, status in (
                (
                    {"mode": "majority", "refs": [ref("session_entry", seeds.session_entry)]},
                    "INVALID_SUPPORT_POLICY",
                    422,
                ),
                ({"mode": "all_required", "refs": []}, "INVALID_SUPPORT_POLICY", 422),
                (
                    {
                        "mode": "all_required",
                        "refs": [{"source_type": "dataset", "source_id": str(seeds.dataset)}],
                    },
                    "INVALID_EVIDENCE_REFERENCE",
                    422,
                ),
                (
                    {
                        "mode": "all_required",
                        "evidence": [ref("session_entry", seeds.session_entry)],
                    },
                    "INVALID_REQUEST",
                    422,
                ),
                (
                    {
                        "mode": "all_required",
                        "refs": [ref("session_entry", seeds.session_entry)] * 2,
                    },
                    "INVALID_REQUEST",
                    422,
                ),
                (
                    {"mode": "all_required", "refs": [ref("chunk", uuid4())]},
                    "EVIDENCE_NOT_FOUND",
                    404,
                ),
            ):
                response = await client.post(
                    "/api/v1/episodes",
                    json={**episode_payload(seeds.scope), "support": bad},
                    headers=keyed(),
                )
                assert response.status_code == status, (bad, response.text)
                assert response.json()["error"]["code"] == code
            cp = await client.post(
                BASE,
                json={
                    "scope": seeds.scope,
                    "current_state": {
                        "text": "s",
                        "support": support("all_required", ref("chunk", seeds.chunk)),
                    },
                    "provenance": {"origin_kind": "user_asserted", "source_system": "t"},
                },
                headers=keyed(),
            )
            assert cp.status_code == 201, cp.text
            assert (
                cp.json()["data"]["current_state"]["support"]["evidence"][0]["availability"]
                == "available"
            )
            for path in ("/api/v1/evidence", "/api/v1/evidence/x"):
                assert (await client.post(path, json={}, headers=keyed())).status_code in {404, 405}
    finally:
        await cleanup(session_factory, seeds)
