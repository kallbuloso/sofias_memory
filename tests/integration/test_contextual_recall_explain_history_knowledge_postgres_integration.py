"""Real-PostgreSQL integration suite for SM-CTX-007 explain/history/Knowledge
(handoff 15 SS 170-182, AC-007.x).

Real PostgreSQL+pgvector, the real Episode/Checkpoint/Cognitive Memory
services and the real Forget authoritative mutation to seed/forget, and the
real HTTP app (``enable_neo4j=False``) to recall. Only the embedding provider
is faked. Knowledge chunks are seeded directly (controlled vectors/lexemes) in
datasets created per test, then removed.

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a
dedicated ``DATABASE_URL`` migrated to head (0022). The Knowledge Forget test
additionally requires a database name ending in ``_test``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointInvalidateRequest,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.episodes import EpisodeInvalidateRequest
from tests.integration.test_context_checkpoints_postgres_integration import (
    HEADERS,
    key,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    payload as checkpoint_payload,
)
from tests.integration.test_contextual_evidence_postgres_integration import (
    checkpoints,
    ref,
    seed,
    session_factory,
    support,
)
from tests.integration.test_contextual_forget_knowledge_postgres_integration import (
    run_source_mutation,
)
from tests.integration.test_contextual_recall_postgres_integration import (
    DIM,
    PATH,
    QUERY,
    FailingEmbeddingClient,
    VectorEmbeddingClient,
    World,
    episode_ids,
    memory_ids,
    recall,
    recall_app,
    row_counts,
    vec,
)

__all__ = ["session_factory"]

LEXICAL_TEXT = "onde paramos no projeto"
"""Contains every lexeme of QUERY under the 'simple' configuration."""


class KnowledgeWorld(World):
    """World plus extra datasets/chunks seeded for Knowledge retrieval."""

    def __init__(self, factory: AsyncSessionFactory, seeds: Any) -> None:
        super().__init__(factory, seeds)
        self.extra_chunks: list[UUID] = []
        self.extra_datasets: list[UUID] = []

    async def slug(self, dataset_id: UUID | None = None) -> str:
        async with self.factory() as s:
            value = await s.scalar(
                text("SELECT slug FROM datasets WHERE id = :d"),
                {"d": dataset_id or self.seeds.dataset},
            )
        return str(value)

    async def chunk(
        self,
        body: str,
        *,
        cosine: float,
        dataset_id: UUID | None = None,
        document_id: UUID | None = None,
        source_id: UUID | None = None,
    ) -> UUID:
        chunk_id = uuid4()
        vector = "[" + ",".join(str(v) for v in vec(cosine)) + "]"
        async with self.factory() as s:
            ordinal = await s.scalar(
                text("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM chunks WHERE document_id = :doc"),
                {"doc": document_id or self.seeds.document},
            )
            await s.execute(
                text(
                    "INSERT INTO chunks (id, dataset_id, document_id, source_id, generation, "
                    "ordinal, text, content_sha256, token_count, start_char, end_char, "
                    "section_path, metadata, embedding, lexical, is_active) VALUES (:id, :d, "
                    ":doc, :src, 1, :o, :t, :sha, 1, 0, 5, '{}', '{}', CAST(:v AS vector), "
                    "to_tsvector('simple', :t), true)"
                ),
                {
                    "id": chunk_id,
                    "d": dataset_id or self.seeds.dataset,
                    "doc": document_id or self.seeds.document,
                    "src": source_id or self.seeds.source,
                    "o": ordinal,
                    "t": body,
                    "sha": uuid4().hex + uuid4().hex,
                    "v": vector,
                },
            )
            await s.commit()
        self.extra_chunks.append(chunk_id)
        return chunk_id

    async def dataset(self, *, status: str = "active") -> tuple[UUID, str, UUID, UUID]:
        dataset_id, source_id, document_id = uuid4(), uuid4(), uuid4()
        slug = f"ctx7-{uuid4().hex[:16]}"
        async with self.factory() as s:
            await s.execute(
                text(
                    "INSERT INTO datasets (id, name, slug, status, active_generation) "
                    "VALUES (:id, :n, :slug, :st, 1)"
                ),
                {"id": dataset_id, "n": slug, "slug": slug, "st": status},
            )
            await s.execute(
                text(
                    "INSERT INTO sources (id, dataset_id, kind, name, mime_type, content_sha256, "
                    "byte_size, metadata, status, version) VALUES (:id, :d, 'text', 'n', "
                    "'text/plain', :sha, 1, '{}', 'active', 1)"
                ),
                {"id": source_id, "d": dataset_id, "sha": "e" * 64},
            )
            await s.execute(
                text(
                    "INSERT INTO documents (id, dataset_id, source_id, generation, title, "
                    "language, normalized_text, text_sha256, token_count, metadata, is_active) "
                    "VALUES (:id, :d, :src, 1, 't', 'pt', 'texto', :sha, 1, '{}', true)"
                ),
                {"id": document_id, "d": dataset_id, "src": source_id, "sha": "f" * 64},
            )
            await s.commit()
        self.extra_datasets.append(dataset_id)
        return dataset_id, slug, source_id, document_id

    async def cleanup(self) -> None:
        async with self.factory() as s:
            await s.execute(text("DELETE FROM chunks WHERE id = ANY(:i)"), {"i": self.extra_chunks})
            await s.commit()
        await super().cleanup()
        async with self.factory() as s:
            ids = self.extra_chunks
            await s.execute(
                text(
                    "DELETE FROM contextual_evidence_refs r WHERE r.source_id = ANY(:i) "
                    "AND NOT EXISTS (SELECT 1 FROM episode_evidence_refs l "
                    "WHERE l.evidence_ref_id = r.id) "
                    "AND NOT EXISTS (SELECT 1 FROM context_statement_evidence_refs l "
                    "WHERE l.evidence_ref_id = r.id)"
                ),
                {"i": ids},
            )
            for table in ("chunks", "documents", "sources", "datasets"):
                column = "id" if table == "datasets" else "dataset_id"
                await s.execute(
                    text(f"DELETE FROM {table} WHERE {column} = ANY(:d)"),  # noqa: S608 - fixed
                    {"d": self.extra_datasets},
                )
            await s.commit()


@pytest_asyncio.fixture()
async def kworld(session_factory: AsyncSessionFactory) -> AsyncIterator[KnowledgeWorld]:
    w = KnowledgeWorld(session_factory, await seed(session_factory))
    try:
        yield w
    finally:
        await w.cleanup()


async def post(app: Any, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(PATH, json=body, headers=HEADERS)


def refs(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data["knowledge_references"])


def support_ids(resource: dict[str, Any] | None) -> list[str]:
    if resource is None or resource.get("support") is None:
        return []
    return [e["source_id"] for e in resource["support"]["evidence"]]


def episode_by_id(data: dict[str, Any], episode_id: UUID) -> dict[str, Any]:
    for item in data["episodes"]:
        if item["episode"]["episode_id"] == str(episode_id):
            return dict(item["episode"])
    raise AssertionError(f"episode {episode_id} not returned")


async def explain_chain(world: KnowledgeWorld) -> tuple[UUID, UUID, UUID, Any]:
    """Chunk K <- Episode A <- Episode B <- current checkpoint.current_state."""

    k = world.seeds.chunk
    a = await world.episode(
        f"A apoiado por K {uuid4().hex}",
        context_key="main",
        cosine=0.9,
        sup=support("all_required", ref("chunk", k)),
    )
    b = await world.episode(
        f"B apoiado por A {uuid4().hex}",
        context_key="main",
        cosine=0.95,
        sup=support("all_required", ref("episode", a)),
    )
    cp = await world.checkpoint(
        current_state={
            "text": f"Estado apoiado por B {uuid4().hex}",
            "support": support("all_required", ref("episode", b)),
        }
    )
    return a, b, k, cp


# --- explain (SS 170/171/153, AC-007.1/3/4/5) ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_returns_the_explicit_bounded_causal_chain(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, k, cp = await explain_chain(kworld)
    noise = await kworld.episode(
        f"Semelhante sem Evidence {uuid4().hex}", context_key="main", cosine=0.5
    )
    client = VectorEmbeddingClient(dict(kworld.client.vectors))
    app = recall_app(kworld.factory, monkeypatch, client)

    data = await recall(app, kworld.scope, intent="explain", include_evidence=True)

    assert client.calls == [[QUERY]]
    assert data["intent"] == "explain" and data["continuity_available"] is True
    assert data["current_checkpoint"]["checkpoint_id"] == str(cp.checkpoint_id)
    assert support_ids(data["current_checkpoint"]["current_state"]) == [str(b)]
    assert support_ids(episode_by_id(data, b)) == [str(a)]
    assert support_ids(episode_by_id(data, a)) == [str(k)]
    assert support_ids(episode_by_id(data, noise)) == []  # similarity is never Evidence
    assert {(e["source_type"], e["source_id"]) for e in data["evidence"]} == {
        ("episode", str(b)),
        ("episode", str(a)),
        ("chunk", str(k)),
    }
    assert data["warnings"] == [] and data["degraded_sources"] == []
    assert data["historical_checkpoints"] == [] and data["knowledge_references"] == []
    assert not {"answer", "explanation", "summary"} & set(data)

    def owners(bundle: dict[str, Any], source_id: UUID) -> list[tuple[str, str, str]]:
        for item in bundle["evidence"]:
            if item["source_id"] == str(source_id):
                return [(o["owner_type"], o["owner_id"], o["support_mode"]) for o in item["owners"]]
        raise AssertionError(f"{source_id} missing from evidence")

    statement_id = data["current_checkpoint"]["current_state"]["statement_id"]
    assert owners(data, b) == [("context_statement", statement_id, "all_required")]
    assert owners(data, a) == [("episode", str(b), "all_required")]
    assert owners(data, k) == [("episode", str(a), "all_required")]

    # Only B is returned in episodes[]: the trace still carries S -> B, B -> A
    # and A -> K through Evidence owners (A's identity only, never its payload).
    single = await recall(
        app, kworld.scope, intent="explain", include_evidence=True, limits={"episodes": 1}
    )
    assert episode_ids(single) == [str(b)]
    assert owners(single, b) == [("context_statement", statement_id, "all_required")]
    assert owners(single, a) == [("episode", str(b), "all_required")]
    assert owners(single, k) == [("episode", str(a), "all_required")]
    assert single["warnings"] == [] and single["degraded_sources"] == []
    a_summary = episode_by_id(data, a)["summary"]
    assert a_summary not in json.dumps(single)  # no intermediate semantic payload

    capped = await recall(
        app,
        kworld.scope,
        intent="explain",
        include_evidence=True,
        limits={"episodes": 1, "evidence": 2},
    )
    assert {e["source_id"] for e in capped["evidence"]} == {str(b), str(a)}  # coherent prefix
    assert capped["warnings"] == ["PARTIAL_CONTEXT"] and capped["degraded_sources"] == []

    plain = await recall(app, kworld.scope, intent="explain", include_evidence=False)
    assert plain["evidence"] == [] and plain["warnings"] == []
    assert "owners" not in json.dumps(plain)  # previous output contract preserved
    assert support_ids(plain["current_checkpoint"]["current_state"]) == [str(b)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_without_evidence_and_lineage_fabricates_nothing(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    rev1 = await kworld.checkpoint()
    rev2 = await checkpoints(kworld.factory).supersede(
        rev1.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(kworld.scope, context_key="main")
        ),
        idempotency_key=key(),
    )
    await kworld.episode(f"Sem suporte {uuid4().hex}", context_key="main", cosine=0.8)
    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    data = await recall(app, kworld.scope, intent="explain", include_evidence=True)
    assert data["current_checkpoint"]["checkpoint_id"] == str(rev2.checkpoint_id)
    assert data["current_checkpoint"]["current_state"]["support"] is None
    assert data["historical_checkpoints"] == []  # rev1 is chronology, never a cause
    assert str(rev1.checkpoint_id) not in json.dumps(data)
    assert data["evidence"] == [] and data["warnings"] == [] and data["degraded_sources"] == []


# --- history (SS 172/176/178, AC-007.2/6-12) ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_separates_current_and_historical_with_lifecycle_and_chronology(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_project = kworld.other_scope()
    svc = checkpoints(kworld.factory)
    rev1 = await kworld.checkpoint()
    rev2 = await svc.supersede(
        rev1.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(kworld.scope, context_key="main")
        ),
        idempotency_key=key(),
    )
    await svc.invalidate(
        rev2.checkpoint_id,
        ContextCheckpointInvalidateRequest(reason="não confiável"),
        idempotency_key=key(),
    )
    rev3 = await kworld.checkpoint()
    gone_marker = uuid4().hex
    gone_rev = await kworld.checkpoint(context_key="feature:gone")
    await svc.forget(gone_rev.checkpoint_id, idempotency_key=key())
    other_stream = await kworld.checkpoint(context_key="feature:x")
    await svc.supersede(
        other_stream.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(kworld.scope, context_key="feature:x")
        ),
        idempotency_key=key(),
    )
    old = await kworld.episode(
        f"Antigo {uuid4().hex}", context_key="main", cosine=0.3, occurred_at="2026-11-01T10:00:00Z"
    )
    invalid = await kworld.episode(
        f"Invalidado {uuid4().hex}",
        context_key=None,
        cosine=0.2,
        occurred_at="2026-11-10T10:00:00Z",
    )
    await kworld.episodes().invalidate(
        invalid, EpisodeInvalidateRequest(reason="errado"), idempotency_key=key()
    )
    forgotten = await kworld.episode(
        f"Esquecido {gone_marker}",
        context_key="main",
        cosine=0.9,
        occurred_at="2026-11-15T10:00:00Z",
    )
    await kworld.episodes().forget(forgotten, idempotency_key=key())
    recent = await kworld.episode(
        f"Recente {uuid4().hex}", context_key="main", cosine=0.1, occurred_at="2026-11-20T10:00:00Z"
    )
    elsewhere = await kworld.episode(
        f"Outro stream {uuid4().hex}", context_key="feature:x", cosine=0.99
    )
    foreign = await kworld.episode(
        f"Outro projeto {uuid4().hex}", context_key="main", scope=other_project, cosine=0.99
    )
    await kworld.memory(f"Verdade atual {uuid4().hex}", cosine=0.99)

    for client in (VectorEmbeddingClient(dict(kworld.client.vectors)), FailingEmbeddingClient()):
        app = recall_app(kworld.factory, monkeypatch, client)
        data = await recall(app, kworld.scope, intent="history")
        assert data["current_checkpoint"]["checkpoint_id"] == str(rev3.checkpoint_id)
        assert [c["checkpoint_id"] for c in data["historical_checkpoints"]] == [
            str(rev2.checkpoint_id),
            str(rev1.checkpoint_id),
        ]
        assert [(c["revision"], c["lifecycle"]) for c in data["historical_checkpoints"]] == [
            (2, "invalidated"),
            (1, "superseded"),
        ]
        assert all(
            c["result_class"] == "historical_checkpoint" for c in data["historical_checkpoints"]
        )
        assert episode_ids(data) == [str(recent), str(invalid), str(old)]  # occurred_at DESC
        lifecycles = {
            e["episode"]["episode_id"]: e["episode"]["lifecycle"] for e in data["episodes"]
        }
        assert lifecycles[str(invalid)] == "invalidated" and lifecycles[str(old)] == "active"
        assert data["current_memories"] == []  # no global/current memory pollution
        rendered = json.dumps(data)
        for excluded in (
            forgotten,
            elsewhere,
            foreign,
            gone_rev.checkpoint_id,
            other_stream.checkpoint_id,
        ):
            assert str(excluded) not in rendered
        assert gone_marker not in rendered
        if isinstance(client, FailingEmbeddingClient):
            assert all(e["relevance"] is None for e in data["episodes"])
            assert data["warnings"] == ["PARTIAL_CONTEXT"]
            assert data["degraded_sources"] == ["episodes"]
            assert client.calls == 1
        else:
            assert data["warnings"] == [] and data["degraded_sources"] == []

    empty_scope = kworld.other_scope()
    healthy = recall_app(kworld.factory, monkeypatch, VectorEmbeddingClient())
    empty = await recall(healthy, empty_scope, intent="history")  # valid, never 404
    assert empty["historical_checkpoints"] == [] and empty["episodes"] == []
    assert empty["warnings"] == ["NO_CURRENT_CHECKPOINT"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_never_reveals_content_scrubbed_by_forget(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    m = await kworld.memory(f"Fonte {marker}", cosine=0.5)
    derived = await kworld.episode(
        f"Derivado {marker}",
        context_key="main",
        cosine=0.9,
        sup=support("all_required", ref("memory_item", m)),
    )
    rev1 = await kworld.checkpoint(
        current_state={"text": f"Estado independente {uuid4().hex}"},
        pending_work=[
            {
                "text": f"Pendência derivada {marker}",
                "support": support("all_required", ref("memory_item", m)),
            },
            {"text": f"Pendência livre {uuid4().hex}"},
        ],
    )
    await checkpoints(kworld.factory).supersede(
        rev1.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(kworld.scope, context_key="main")
        ),
        idempotency_key=key(),
    )
    await kworld.memories().forget(m, idempotency_key=key("fm"))

    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    for intent in ("history", "explain", "resume", "context"):
        data = await recall(app, kworld.scope, intent=intent, include_evidence=True)
        rendered = json.dumps(data)
        assert marker not in rendered, intent
        assert str(derived) not in episode_ids(data)
    history = await recall(app, kworld.scope, intent="history")
    (hist,) = history["historical_checkpoints"]
    assert hist["checkpoint_id"] == str(rev1.checkpoint_id)
    assert len(hist["pending_work"]) == 1  # only the statement that still exists


# --- Knowledge (SS 173-175, AC-007.13-15/21-26) ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_only_with_explicit_datasets_and_never_chunk_text(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = f"conteúdo integral {uuid4().hex}"
    best = await kworld.chunk(f"{LEXICAL_TEXT} {secret}", cosine=0.9)
    second = await kworld.chunk(f"outro trecho {uuid4().hex}", cosine=0.8)
    other_ds, other_slug, other_src, other_doc = await kworld.dataset()
    foreign = await kworld.chunk(
        f"{LEXICAL_TEXT} {uuid4().hex}",
        cosine=0.99,
        dataset_id=other_ds,
        document_id=other_doc,
        source_id=other_src,
    )
    slug = await kworld.slug()
    await kworld.checkpoint()
    client = VectorEmbeddingClient(dict(kworld.client.vectors))
    app = recall_app(kworld.factory, monkeypatch, client)

    for intent in ("resume", "context", "explain", "history"):
        without = await recall(app, kworld.scope, intent=intent)
        assert without["knowledge_references"] == []  # no datasets -> no Knowledge at all
        with_ds = await recall(app, kworld.scope, intent=intent, datasets=[slug])
        chunk_ids = [r["chunk_id"] for r in refs(with_ds)]
        assert chunk_ids[:2] == [str(best), str(second)]  # vector + lexical, local rank
        assert str(foreign) not in chunk_ids  # only the selected dataset
        assert [r["rank"] for r in refs(with_ds)] == list(range(1, len(chunk_ids) + 1))
        assert all(r["result_class"] == "knowledge_reference" for r in refs(with_ds))
        assert all(r["dataset_slug"] == slug and not r["evidence_backed"] for r in refs(with_ds))
        assert secret not in json.dumps(with_ds)  # identities only, never chunk text
        assert with_ds["evidence"] == []  # a retrieval hit is never Evidence
    assert len(client.calls) == 8  # exactly one provider call per request

    both = await recall(app, kworld.scope, intent="context", datasets=[slug, other_slug])
    assert str(foreign) in [r["chunk_id"] for r in refs(both)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_or_inactive_dataset_is_an_explicit_422(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, deleting_slug, _, _ = await kworld.dataset(status="deleting")
    slug = await kworld.slug()
    before = await row_counts(kworld.factory)
    app = recall_app(kworld.factory, monkeypatch, VectorEmbeddingClient())
    for bad in (f"nao-existe-{uuid4().hex[:8]}", deleting_slug):
        response = await post(
            app,
            {"scope": kworld.scope, "intent": "history", "query": QUERY, "datasets": [slug, bad]},
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert error["details"] == {"field": "datasets", "dataset": bad}
    async with kworld.factory() as s:
        created = await s.scalar(
            text("SELECT count(*) FROM datasets WHERE slug LIKE 'nao-existe-%'")
        )
    assert created == 0  # never created
    assert await row_counts(kworld.factory) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_embedding_failure_keeps_lexical_knowledge_and_signals_once(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    lexical = await kworld.chunk(f"{LEXICAL_TEXT} {uuid4().hex}", cosine=0.1)
    vector_only = await kworld.chunk(f"sem termos {uuid4().hex}", cosine=0.99)
    cp = await kworld.checkpoint()
    survivor = await kworld.episode(f"Recente {uuid4().hex}", context_key="main", cosine=0.5)
    slug = await kworld.slug()
    client = FailingEmbeddingClient()
    app = recall_app(kworld.factory, monkeypatch, client)
    data = await recall(app, kworld.scope, intent="context", datasets=[slug])
    assert client.calls == 1  # the single failed attempt, never retried
    assert data["current_checkpoint"]["checkpoint_id"] == str(cp.checkpoint_id)
    assert str(survivor) in episode_ids(data)
    chunk_ids = [r["chunk_id"] for r in refs(data)]
    assert chunk_ids == [str(lexical)]  # lexical-only, never presented as semantic
    assert str(vector_only) not in chunk_ids
    assert data["warnings"] == ["PARTIAL_CONTEXT"]
    assert data["degraded_sources"] == ["memories", "episodes", "knowledge"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_backed_knowledge_stays_distinct_and_inside_selected_datasets(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_ds, other_slug, other_src, other_doc = await kworld.dataset()
    foreign_chunk = await kworld.chunk(
        f"fora {uuid4().hex}",
        cosine=0.0,
        dataset_id=other_ds,
        document_id=other_doc,
        source_id=other_src,
    )
    k = kworld.seeds.chunk
    grounded = await kworld.episode(
        f"Com Evidence {uuid4().hex}",
        context_key="main",
        cosine=0.9,
        sup=support("all_required", ref("chunk", k), ref("chunk", foreign_chunk)),
    )
    slug = await kworld.slug()
    app = recall_app(kworld.factory, monkeypatch, FailingEmbeddingClient())
    data = await recall(app, kworld.scope, intent="explain", datasets=[slug], include_evidence=True)
    assert str(grounded) in episode_ids(data)
    backed = [r for r in refs(data) if r["evidence_backed"]]
    assert [(r["chunk_id"], r["rank"], r["relevance"]) for r in backed] == [(str(k), None, None)]
    assert str(foreign_chunk) not in [r["chunk_id"] for r in refs(data)]
    assert {e["source_id"] for e in data["evidence"]} == {str(k), str(foreign_chunk)}


# --- Knowledge Forget, isolation, read-only, restart (SS 176-182) ----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_forget_propagates_and_no_intent_reconstructs_it(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with kworld.factory() as s:
        name = str(await s.scalar(text("SELECT current_database()")))
    if not name.endswith("_test"):
        pytest.skip("Knowledge Forget requires a dedicated *_test database")
    a, b, k, _ = await explain_chain(kworld)
    marker_a = (await kworld.episodes().get(a)).summary
    slug = await kworld.slug()
    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    before = await recall(app, kworld.scope, intent="explain", datasets=[slug])
    assert str(k) in [r["chunk_id"] for r in refs(before) if r["evidence_backed"]]

    await run_source_mutation(
        kworld.factory,
        dataset_slug=slug,
        dataset_id=kworld.seeds.dataset,
        source_id=kworld.seeds.source,
    )

    for intent in ("explain", "history", "resume", "context"):
        data = await recall(app, kworld.scope, intent=intent, include_evidence=True)
        rendered = json.dumps(data)
        assert marker_a not in rendered, intent
        assert str(a) not in episode_ids(data) and str(b) not in episode_ids(data)
        assert data["current_checkpoint"] is None  # current_state lost its support chain
    after = await recall(app, kworld.scope, intent="context", datasets=[slug])
    assert str(k) not in [r["chunk_id"] for r in refs(after)]  # forgotten Knowledge never returns


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_project_and_cross_stream_isolation(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = kworld.other_scope()
    global_memory = await kworld.memory(f"Global {uuid4().hex}", scope="global", cosine=0.99)
    await kworld.checkpoint(
        current_state={
            "text": "estado",
            "support": support("all_required", ref("memory_item", global_memory)),
        }
    )
    foreign_cp = await kworld.checkpoint(scope=other)
    await checkpoints(kworld.factory).supersede(
        foreign_cp.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(other, context_key="main")
        ),
        idempotency_key=key(),
    )
    foreign_ep = await kworld.episode(
        f"Outro {uuid4().hex}", context_key="main", scope=other, cosine=0.99
    )
    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    for intent in ("resume", "context", "explain", "history"):
        data = await recall(app, kworld.scope, intent=intent, include_evidence=True)
        rendered = json.dumps(data)
        assert str(foreign_cp.checkpoint_id) not in rendered and str(foreign_ep) not in rendered
        assert str(global_memory) not in memory_ids(data)  # Evidence reference only
        assert str(global_memory) in [e["source_id"] for e in data["evidence"]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_intent_is_read_only_and_restart_stable(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await explain_chain(kworld)
    await kworld.chunk(f"{LEXICAL_TEXT} {uuid4().hex}", cosine=0.7)
    slug = await kworld.slug()
    vectors = dict(kworld.client.vectors)

    async def snapshot_counts() -> dict[str, int]:
        counts = await row_counts(kworld.factory)
        async with kworld.factory() as s:
            for table in (
                "episode_evidence_refs",
                "context_statement_evidence_refs",
                "datasets",
                "chunks",
            ):
                counts[table] = int(await s.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
        return counts

    before = await snapshot_counts()
    first: dict[str, Any] = {}
    for client in (VectorEmbeddingClient(vectors), FailingEmbeddingClient()):
        app = recall_app(kworld.factory, monkeypatch, client)
        for intent in ("resume", "context", "explain", "history"):
            result = await recall(
                app, kworld.scope, intent=intent, datasets=[slug], include_evidence=True
            )
            if isinstance(client, VectorEmbeddingClient):
                first[intent] = result
    assert await snapshot_counts() == before  # zero writes of any kind

    engine = create_async_engine_from_settings(load_settings())  # a fresh process boundary
    try:
        app = recall_app(
            create_session_factory(engine), monkeypatch, VectorEmbeddingClient(vectors)
        )
        for intent in ("explain", "history"):
            again = await recall(
                app, kworld.scope, intent=intent, datasets=[slug], include_evidence=True
            )
            assert again == first[intent]  # structurally equivalent after restart
    finally:
        await dispose_async_engine(engine)


def test_dimension_constant_matches_the_shared_fixture() -> None:
    assert DIM == 3072


# --- PR #4 corrections: review findings B and C ------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_intermediate_declared_support_without_links_is_partial_and_read_only(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding B through the real support hydration: A keeps its persisted
    support_mode but has no Evidence link (controlled corruption of this
    disposable database). The trace keeps the persisted edges, never invents
    A -> K, signals PARTIAL_CONTEXT and writes nothing."""

    a, b, k, _ = await explain_chain(kworld)
    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    async with kworld.factory() as s:
        await s.execute(text("DELETE FROM episode_evidence_refs WHERE episode_id = :a"), {"a": a})
        await s.commit()
    async with kworld.factory() as s:
        mode = await s.scalar(text("SELECT support_mode FROM episodes WHERE id = :a"), {"a": a})
        a_summary = await s.scalar(text("SELECT summary FROM episodes WHERE id = :a"), {"a": a})
    assert mode == "all_required"
    before = await row_counts(kworld.factory)

    data = await recall(
        app, kworld.scope, intent="explain", include_evidence=True, limits={"episodes": 1}
    )

    assert episode_ids(data) == [str(b)]  # A is an intermediate, not returned
    assert {(e["source_type"], e["source_id"]) for e in data["evidence"]} == {
        ("episode", str(b)),
        ("episode", str(a)),
    }
    a_item = next(e for e in data["evidence"] if e["source_id"] == str(a))
    assert [(o["owner_type"], o["owner_id"]) for o in a_item["owners"]] == [("episode", str(b))]
    assert str(k) not in json.dumps(data["evidence"])  # no invented Evidence
    assert data["warnings"] == ["PARTIAL_CONTEXT"] and data["degraded_sources"] == []
    assert a_summary not in json.dumps(data)  # no intermediate semantic payload
    assert await row_counts(kworld.factory) == before  # read-only
    async with kworld.factory() as s:
        links = await s.scalar(
            text("SELECT count(*) FROM episode_evidence_refs WHERE episode_id = :a"), {"a": a}
        )
        mode_after = await s.scalar(
            text("SELECT support_mode FROM episodes WHERE id = :a"), {"a": a}
        )
    assert links == 0 and mode_after == "all_required"  # never repaired


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_discarded_candidates_never_consume_trace_capacity(
    kworld: KnowledgeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding C with valid data: limits.episodes=8 reads 24 candidates; the 16
    discarded ones carry 17 Episode Evidence refs each (272 > 256 Episodes).
    The returned checkpoint depends on X -> Y -> Chunk K. Rooted at the
    candidate window the trace was cut (K missing, PARTIAL_CONTEXT); rooted at
    the selected resources it is complete."""

    k = kworld.seeds.chunk
    tag = uuid4().hex
    y = await kworld.episode(
        f"Y {tag}", context_key="main", cosine=0.01, sup=support("all_required", ref("chunk", k))
    )
    x = await kworld.episode(
        f"X {tag}", context_key="main", cosine=0.01, sup=support("all_required", ref("episode", y))
    )
    returned = [
        await kworld.episode(f"Selecionado {i} {tag}", context_key="main", cosine=0.9 - i * 0.01)
        for i in range(8)
    ]
    for i in range(16):
        parents = [
            await kworld.episode(f"Pai {i}-{j} {tag}", context_key="main", cosine=0.05)
            for j in range(17)
        ]
        await kworld.episode(
            f"Descartado {i} {tag}",
            context_key="main",
            cosine=0.5 - i * 0.01,
            sup=support("any_sufficient", *(ref("episode", p) for p in parents)),
        )
    await kworld.checkpoint(
        current_state={
            "text": f"Estado apoiado por X {tag}",
            "support": support("all_required", ref("episode", x)),
        }
    )
    app = recall_app(
        kworld.factory, monkeypatch, VectorEmbeddingClient(dict(kworld.client.vectors))
    )
    before = await row_counts(kworld.factory)

    data = await recall(
        app, kworld.scope, intent="explain", include_evidence=True, limits={"episodes": 8}
    )

    assert episode_ids(data) == [str(e) for e in returned]
    by_id = {e["source_id"]: e for e in data["evidence"]}
    assert set(by_id) == {str(x), str(y), str(k)}  # only the selected trace
    assert [(o["owner_type"], o["owner_id"]) for o in by_id[str(k)]["owners"]] == [
        ("episode", str(y))
    ]
    assert data["warnings"] == [] and data["degraded_sources"] == []  # no spurious partial
    again = await recall(
        app, kworld.scope, intent="explain", include_evidence=True, limits={"episodes": 8}
    )
    assert again["evidence"] == data["evidence"]  # deterministic
    assert await row_counts(kworld.factory) == before
