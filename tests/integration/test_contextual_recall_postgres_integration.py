"""Real-PostgreSQL integration suite for SM-CTX-006 Contextual Recall Core
(handoff 14 SS 64.1/125-138, AC-006.x).

Real PostgreSQL+pgvector, the real Episode/Checkpoint/Cognitive Memory
services to seed, and the real HTTP app (``enable_neo4j=False``, no Neo4j
resource at all) to recall. Only the embedding provider is faked, with
controllable vectors and call counting.

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a
dedicated ``DATABASE_URL`` migrated to head (0022) -- the same opt-in as the
other contextual suites. Every test uses its own random Project Scopes and
removes the rows it created.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
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
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    ContextCheckpoint,
    ContextCheckpointStatement,
    ContextualEvidenceRef,
    ContextualMemoryIdempotency,
    Episode,
    GraphOutbox,
    MemoryItem,
    PipelineRun,
    PipelineStep,
    Query,
    Session,
    SessionEntry,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointInvalidateRequest,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.episodes import EpisodeInvalidateRequest
from sofias_memory.schemas.memories import MemoryCreateRequest
from sofias_memory.services.cognitive_memory import CognitiveMemoryService
from sofias_memory.services.contextual_evidence_invalidator import (
    ContextualEvidenceInvalidator,
    PropagationCounts,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    HEADERS,
    count,
    key,
    make_settings,
    new_scope,
)
from tests.integration.test_context_checkpoints_postgres_integration import (
    payload as checkpoint_payload,
)
from tests.integration.test_contextual_evidence_postgres_integration import (
    Seeds,
    TrackingEpisodeService,
    checkpoints,
    cleanup,
    episode_request,
    ref,
    seed,
    session_factory,
    support,
)
from tests.unit._app_factory import create_app

__all__ = ["session_factory"]

DIM = 3072
PATH = "/api/v1/context/recall"
QUERY = "Onde paramos no projeto?"


# --- fakes ----------------------------------------------------------------------------------


def vec(cosine: float) -> list[float]:
    """Unit vector whose cosine with the query axis (e0) is ``cosine``."""

    vector = [0.0] * DIM
    vector[0] = cosine
    vector[1] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return vector


class VectorEmbeddingClient:
    """Maps a query, a MemoryItem content or an Episode summary (first line of
    its canonical text) to a controllable vector; counts every call."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors: dict[str, list[float]] = {QUERY: vec(1.0), **(vectors or {})}
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for value in texts:
            first = value.split("\n", 1)[0].removeprefix("Summary: ")
            out.append(self.vectors.get(first, vec(0.1)))
        return out


class FailingEmbeddingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        self.calls += 1
        raise ConnectionError("simulated embedding provider outage")


# --- seeding ----------------------------------------------------------------------------------


class World:
    """Seeds of one test plus every MemoryItem it created (for cleanup)."""

    def __init__(self, factory: AsyncSessionFactory, seeds: Seeds) -> None:
        self.factory = factory
        self.seeds = seeds
        self.scope = seeds.scope
        self.client = VectorEmbeddingClient()
        self.memory_ids: list[UUID] = [seeds.memory]
        self.extra_scopes: list[str] = []

    def episodes(self) -> TrackingEpisodeService:
        return TrackingEpisodeService(
            make_settings(), embedding_client=self.client, session_factory=self.factory
        )  # type: ignore[arg-type]

    def memories(self) -> CognitiveMemoryService:
        return CognitiveMemoryService(
            make_settings(), embedding_client=self.client, session_factory=self.factory
        )  # type: ignore[arg-type]

    async def memory(
        self, content: str, *, scope: str | None = None, cosine: float = 0.1, kind: str = "semantic"
    ) -> UUID:
        self.client.vectors[content] = vec(cosine)
        result = await self.memories().create(
            MemoryCreateRequest.model_validate(
                {
                    "memory_type": kind,
                    "scope": scope or self.scope,
                    "content": content,
                    "provenance": {
                        "origin_kind": "user_asserted",
                        "source_system": "recall-test",
                        "source_ref": "recall-test-fixture",
                    },
                }
            ),
            idempotency_key=None,
        )
        self.memory_ids.append(result.memory_id)
        return cast(UUID, result.memory_id)

    async def episode(
        self,
        summary: str,
        *,
        context_key: str | None,
        scope: str | None = None,
        cosine: float = 0.1,
        occurred_at: str = "2026-11-18T20:00:00Z",
        sup: dict[str, Any] | None = None,
    ) -> UUID:
        self.client.vectors[summary] = vec(cosine)
        overrides: dict[str, Any] = {
            "summary": summary,
            "context_key": context_key,
            "occurred_at": occurred_at,
        }
        if sup is not None:
            overrides["support"] = sup
        result = await self.episodes().create(
            episode_request(scope or self.scope, **overrides), idempotency_key=key()
        )
        return cast(UUID, result.episode_id)

    async def checkpoint(
        self, *, context_key: str = "main", scope: str | None = None, **overrides: Any
    ) -> Any:
        body = checkpoint_payload(scope or self.scope, context_key=context_key, **overrides)
        return await checkpoints(self.factory).create_current(
            ContextCheckpointCreateRequest.model_validate(body), idempotency_key=key()
        )

    def other_scope(self) -> str:
        scope = new_scope()
        self.extra_scopes.append(scope)
        return scope

    async def cleanup(self) -> None:
        await cleanup(self.factory, self.seeds, self.extra_scopes)
        async with self.factory() as s:
            await s.execute(
                text(
                    "DELETE FROM cognitive_memory_idempotency WHERE result_memory_id = ANY(:i) "
                    "OR target_memory_id = ANY(:i)"
                ),
                {"i": self.memory_ids},
            )
            await s.execute(
                text(
                    "DELETE FROM contextual_evidence_refs r WHERE r.source_id = ANY(:i) "
                    "AND NOT EXISTS (SELECT 1 FROM episode_evidence_refs l "
                    "WHERE l.evidence_ref_id = r.id) "
                    "AND NOT EXISTS (SELECT 1 FROM context_statement_evidence_refs l "
                    "WHERE l.evidence_ref_id = r.id)"
                ),
                {"i": self.memory_ids},
            )
            await s.execute(
                text("DELETE FROM memory_items WHERE id = ANY(:i)"), {"i": self.memory_ids}
            )
            await s.commit()


@pytest_asyncio.fixture()
async def world(session_factory: AsyncSessionFactory) -> AsyncIterator[World]:
    w = World(session_factory, await seed(session_factory))
    try:
        yield w
    finally:
        await w.cleanup()


def recall_app(factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch, client: Any) -> Any:
    monkeypatch.setattr(
        "sofias_memory.api.routes.context_recall.OpenAIEmbeddingClient", lambda settings: client
    )
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=factory,
    )


async def recall(app: Any, scope: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"scope": scope, "intent": "resume", "query": QUERY}
    body.update(overrides)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def episode_ids(data: dict[str, Any]) -> list[str]:
    return [item["episode"]["episode_id"] for item in data["episodes"]]


def memory_ids(data: dict[str, Any]) -> list[str]:
    return [item["memory"]["memory_id"] for item in data["current_memories"]]


READ_ONLY_MODELS: tuple[type, ...] = (
    Query,
    PipelineRun,
    PipelineStep,
    Session,
    SessionEntry,
    MemoryItem,
    Episode,
    ContextCheckpoint,
    ContextCheckpointStatement,
    GraphOutbox,
    ContextualMemoryIdempotency,
    CognitiveMemoryIdempotency,
    ContextualEvidenceRef,
)


async def row_counts(factory: AsyncSessionFactory) -> dict[str, int]:
    return {model.__name__: await count(factory, model) for model in READ_ONLY_MODELS}


# --- resume / boundaries (AC-006.1/2/4-9/11-13) -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_composes_checkpoint_project_memories_and_eligible_episodes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_project = world.other_scope()
    cp = await world.checkpoint()
    p1 = await world.memory(f"Preferência do projeto {uuid4().hex}", cosine=0.9, kind="profile")
    p2 = await world.memory(f"Fato do projeto {uuid4().hex}", cosine=0.8)
    g = await world.memory(f"Memória global {uuid4().hex}", scope="global", cosine=0.99)
    b_mem = await world.memory(f"Memória do outro {uuid4().hex}", scope=other_project, cosine=0.99)
    a = await world.episode(f"A project-wide {uuid4().hex}", context_key=None, cosine=0.7)
    b = await world.episode(f"B main {uuid4().hex}", context_key="main", cosine=0.6)
    c = await world.episode(f"C feature-y {uuid4().hex}", context_key="feature:y", cosine=0.99)
    d = await world.episode(
        f"D outro projeto {uuid4().hex}", context_key="main", scope=other_project, cosine=0.99
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("Contextual Recall must not build Knowledge Recall or an LLM")

    monkeypatch.setattr("sofias_memory.services.recall.RecallService.__init__", explode)
    monkeypatch.setattr("sofias_memory.infrastructure.llm.OpenAIRagAnswerClient.__init__", explode)
    client = VectorEmbeddingClient(dict(world.client.vectors))
    app = recall_app(world.factory, monkeypatch, client)
    assert getattr(app.state, "neo4j_resource", None) is None

    data = await recall(app, world.scope)

    assert client.calls == [[QUERY]]  # one query embedding for memories + Episodes
    assert data["scope"] == world.scope and data["context_key"] == "main"
    assert data["intent"] == "resume"
    assert data["continuity_available"] is True
    checkpoint = data["current_checkpoint"]
    assert checkpoint["checkpoint_id"] == str(cp.checkpoint_id)
    assert checkpoint["result_class"] == "current_checkpoint"
    assert checkpoint["lifecycle"] == "active"
    assert checkpoint["next_action"]["text"] == cp.next_action.text
    assert memory_ids(data)[:2] == [str(p1), str(p2)]  # relevance DESC
    assert set(memory_ids(data)) == {str(p1), str(p2), str(world.seeds.memory)}
    assert all(m["result_class"] == "current_semantic" for m in data["current_memories"])
    assert all(m["memory"]["scope"] == world.scope for m in data["current_memories"])
    assert episode_ids(data) == [str(a), str(b)]  # relevance DESC
    assert all(e["result_class"] == "historical_episode" for e in data["episodes"])
    assert all(isinstance(e["relevance"], float) for e in data["episodes"])
    rendered = json.dumps(data)
    for excluded in (g, b_mem, c, d):
        assert str(excluded) not in rendered
    assert data["warnings"] == [] and data["degraded_sources"] == []
    assert data["historical_checkpoints"] == [] and data["knowledge_references"] == []
    assert data["evidence"] == []
    assert "answer" not in data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_ranks_semantically_with_deterministic_ties_and_per_source_bounds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.checkpoint(context_key="feature:a")
    mems = [
        await world.memory(f"Memória {i} {uuid4().hex}", cosine=0.9 - i * 0.05) for i in range(12)
    ]
    e_old = await world.episode(
        f"Empate antigo {uuid4().hex}",
        context_key="feature:a",
        cosine=0.95,
        occurred_at="2026-11-10T10:00:00Z",
    )
    e_new = await world.episode(
        f"Empate novo {uuid4().hex}",
        context_key="feature:a",
        cosine=0.95,
        occurred_at="2026-11-12T10:00:00Z",
    )
    filler = [
        await world.episode(f"Episódio {i} {uuid4().hex}", context_key=None, cosine=0.5 - i * 0.02)
        for i in range(12)
    ]
    client = VectorEmbeddingClient(dict(world.client.vectors))
    app = recall_app(world.factory, monkeypatch, client)

    first = await recall(
        app,
        world.scope,
        intent="context",
        context_key="feature:a",
        limits={"memories": 3, "episodes": 4},
    )
    second = await recall(
        app,
        world.scope,
        intent="context",
        context_key="feature:a",
        limits={"memories": 3, "episodes": 4},
    )

    assert memory_ids(first) == [str(m) for m in mems[:3]]
    # relevance DESC, then occurred_at DESC for equal relevance, then id ASC.
    assert episode_ids(first) == [str(e_new), str(e_old), str(filler[0]), str(filler[1])]
    relevances = [e["relevance"] for e in first["episodes"]]
    assert relevances == sorted(relevances, reverse=True)
    assert first["current_checkpoint"] is not None  # exactly one, never budget-consumed
    assert (memory_ids(second), episode_ids(second)) == (memory_ids(first), episode_ids(first))
    assert len(client.calls) == 2  # one per request

    defaults = await recall(app, world.scope, intent="context", context_key="feature:a")
    assert len(defaults["current_memories"]) == 8 and len(defaults["episodes"]) == 8
    clamped = await recall(
        app,
        world.scope,
        intent="context",
        context_key="feature:a",
        limits={"memories": 10_000, "episodes": 10_000},
    )
    # 12 memories + the seed's own project memory; 14 eligible Episodes.
    assert len(clamped["current_memories"]) == 13 and len(clamped["episodes"]) == 14


# --- continuity (AC-006.3, handoff 14 SS 30-34/127/128) -----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_current_checkpoint_is_a_valid_non_degraded_bundle(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = await world.episode(f"Sem checkpoint {uuid4().hex}", context_key="main", cosine=0.5)
    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    data = await recall(app, world.scope)
    assert data["continuity_available"] is False
    assert data["current_checkpoint"] is None
    assert data["warnings"] == ["NO_CURRENT_CHECKPOINT"]
    assert data["degraded_sources"] == []
    assert str(ep) in episode_ids(data)
    assert str(world.seeds.memory) in memory_ids(data)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_historical_checkpoint_fallback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rev1 = await world.checkpoint()
    rev2 = await checkpoints(world.factory).supersede(
        rev1.checkpoint_id,
        ContextCheckpointSupersedeRequest.model_validate(
            checkpoint_payload(world.scope, context_key="main")
        ),
        idempotency_key=key(),
    )
    await checkpoints(world.factory).invalidate(
        rev2.checkpoint_id,
        ContextCheckpointInvalidateRequest(reason="estado não vale mais"),
        idempotency_key=key(),
    )
    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient())
    for intent in ("resume", "context"):
        data = await recall(app, world.scope, intent=intent)
        assert data["current_checkpoint"] is None
        assert data["continuity_available"] is False
        assert data["warnings"] == ["NO_CURRENT_CHECKPOINT"]
        rendered = json.dumps(data)
        assert str(rev1.checkpoint_id) not in rendered and str(rev2.checkpoint_id) not in rendered
        assert data["historical_checkpoints"] == []


# --- Forget safety (AC-006.22, handoff 14 SS 99-103/129) ----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forgotten_content_never_returns(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    m = await world.memory(f"Fonte esquecida {marker}", cosine=0.9)
    direct = await world.memory(f"Memória esquecida diretamente {marker}", cosine=0.95)
    dep = await world.episode(
        f"Derivado da fonte {marker}",
        context_key="main",
        cosine=0.9,
        sup=support("all_required", ref("memory_item", m)),
    )
    gone = await world.episode(f"Episódio esquecido {marker}", context_key=None, cosine=0.9)
    survivor = await world.episode(f"Sobrevivente {uuid4().hex}", context_key=None, cosine=0.4)
    await world.checkpoint(
        current_state={"text": f"Estado independente {uuid4().hex}"},
        pending_work=[
            {
                "text": f"Pendência derivada {marker}",
                "support": support("all_required", ref("memory_item", m)),
            },
        ],
    )
    await world.memories().forget(m, idempotency_key=key("fm"))
    await world.memories().forget(direct, idempotency_key=key("fm"))
    await world.episodes().forget(gone, idempotency_key=key("fe"))

    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    for include_evidence in (False, True):
        data = await recall(app, world.scope, include_evidence=include_evidence)
        rendered = json.dumps(data)
        assert marker not in rendered
        for forgotten in (m, direct, dep, gone):
            assert str(forgotten) not in episode_ids(data) + memory_ids(data)
        assert str(survivor) in episode_ids(data)
        assert data["continuity_available"] is True
        assert data["current_checkpoint"]["pending_work"] == []


# --- Evidence (AC-006.23, handoff 14 SS 65-68/130/138) -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_identities_are_deduplicated_and_never_expand_content(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = world.seeds
    shared = support("any_sufficient", ref("session_entry", s.session_entry))
    e1 = await world.episode(f"E1 {uuid4().hex}", context_key="main", cosine=0.9, sup=shared)
    e2 = await world.episode(
        f"E2 {uuid4().hex}",
        context_key=None,
        cosine=0.8,
        sup=support("all_required", ref("session_entry", s.session_entry), ref("chunk", s.chunk)),
    )
    await world.checkpoint(current_state={"text": "Estado com evidência", "support": shared})
    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))

    data = await recall(app, world.scope, include_evidence=True)
    assert episode_ids(data)[:2] == [str(e1), str(e2)]
    identities = [(e["source_type"], e["source_id"]) for e in data["evidence"]]
    assert identities == sorted({("chunk", str(s.chunk)), ("session_entry", str(s.session_entry))})
    assert all(e["availability"] == "available" for e in data["evidence"])
    assert all(e["result_class"] == "evidence" for e in data["evidence"])
    rendered = json.dumps(data)
    assert "conteúdo de sessão" not in rendered  # SessionEntry content never expanded
    assert "texto" not in json.dumps(data["evidence"])  # chunk text never expanded

    capped = await recall(app, world.scope, include_evidence=True, limits={"evidence": 1})
    assert [(e["source_type"], e["source_id"]) for e in capped["evidence"]] == identities[:1]

    without = await recall(app, world.scope, include_evidence=False)
    assert without["evidence"] == []
    refs = without["episodes"][0]["episode"]["support"]["evidence"]
    assert [(r["source_type"], r["availability"]) for r in refs] == [("session_entry", "available")]
    assert without["current_checkpoint"]["current_state"]["support"] is not None


# --- support eligibility vs lifecycle (handoff 14 SS 63/64.1, AC-006.25) -------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsupported_active_resources_are_excluded_without_any_mutation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    eps = world.episodes()
    a = await world.episode(f"A base {uuid4().hex}", context_key="main", cosine=0.5)
    b = await world.episode(
        f"B depende de A {uuid4().hex}",
        context_key="main",
        cosine=0.9,
        sup=support("all_required", ref("episode", a)),
    )
    b_any = await world.episode(
        f"B any {uuid4().hex}",
        context_key="main",
        cosine=0.8,
        sup=support(
            "any_sufficient", ref("episode", a), ref("session_entry", world.seeds.session_entry)
        ),
    )
    cp = await world.checkpoint(
        current_state={"text": f"Estado independente {uuid4().hex}"},
        pending_work=[
            {
                "text": "Pendência dependente de A",
                "support": support("all_required", ref("episode", a)),
            },
            {"text": "Pendência independente"},
        ],
    )
    await eps.invalidate(a, EpisodeInvalidateRequest(reason="não vale mais"), idempotency_key=key())
    before = await row_counts(world.factory)
    b_before = await eps.get(b)
    assert b_before.lifecycle.value == "active"  # physically ACTIVE
    assert b_before.support is not None
    assert b_before.support.evidence[0].availability.value == "unavailable"

    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    for include_evidence in (False, True):
        for intent in ("resume", "context"):
            data = await recall(app, world.scope, intent=intent, include_evidence=include_evidence)
            assert str(b) not in episode_ids(data) and str(a) not in episode_ids(data)
            assert str(b_any) in episode_ids(data)  # another AVAILABLE Evidence suffices
            checkpoint = data["current_checkpoint"]
            assert checkpoint["checkpoint_id"] == str(cp.checkpoint_id)
            assert [s["text"] for s in checkpoint["pending_work"]] == ["Pendência independente"]
            assert data["continuity_available"] is True
            assert data["warnings"] == ["PARTIAL_CONTEXT", "EVIDENCE_UNAVAILABLE"]
            assert data["degraded_sources"] == []

    assert await row_counts(world.factory) == before
    assert await eps.get(b) == b_before  # never repaired during read
    persisted = await checkpoints(world.factory).get(cp.checkpoint_id)
    assert len(persisted.pending_work) == 2 and persisted.lifecycle.value == "active"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsupported_current_state_means_no_continuity_and_no_fallback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await world.episode(f"A base {uuid4().hex}", context_key="main", cosine=0.5)
    cp = await world.checkpoint(
        current_state={
            "text": f"Estado dependente {uuid4().hex}",
            "support": support("all_required", ref("episode", a)),
        }
    )
    await world.episodes().invalidate(
        a, EpisodeInvalidateRequest(reason="não vale mais"), idempotency_key=key()
    )
    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    for include_evidence in (False, True):
        data = await recall(app, world.scope, include_evidence=include_evidence)
        assert data["current_checkpoint"] is None
        assert data["continuity_available"] is False
        assert data["warnings"] == [
            "NO_CURRENT_CHECKPOINT",
            "PARTIAL_CONTEXT",
            "EVIDENCE_UNAVAILABLE",
        ]
        assert str(cp.checkpoint_id) not in json.dumps(data)
    persisted = await checkpoints(world.factory).get(cp.checkpoint_id)
    assert persisted.lifecycle.value == "active"  # the row stays physically ACTIVE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_forget_invariant_failure_never_exposes_content(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    m = await world.memory(f"Fonte {uuid4().hex}", cosine=0.2)
    leaked = await world.episode(
        f"Não deveria existir {marker}",
        context_key="main",
        cosine=0.99,
        sup=support("all_required", ref("memory_item", m)),
    )
    await world.checkpoint(
        current_state={"text": f"Estado {uuid4().hex}"},
        open_items=[
            {
                "text": f"Item que deveria ter sido removido {marker}",
                "support": support("all_required", ref("memory_item", m)),
            },
        ],
    )

    async def skip(self: Any, seeds: Any, **kwargs: Any) -> PropagationCounts:
        return PropagationCounts()

    # Injected invariant failure: the Forget commits without propagation.
    monkeypatch.setattr(ContextualEvidenceInvalidator, "propagate", skip)
    await world.memories().forget(m, idempotency_key=key("fm"))
    monkeypatch.undo()
    assert (await world.episodes().get(leaked)).lifecycle.value == "active"

    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    before = await row_counts(world.factory)
    for include_evidence in (False, True):
        data = await recall(app, world.scope, include_evidence=include_evidence)
        assert marker not in json.dumps(data)
        assert str(leaked) not in episode_ids(data)
        assert "EVIDENCE_UNAVAILABLE" in data["warnings"]
    assert await row_counts(world.factory) == before
    assert (await world.episodes().get(leaked)).lifecycle.value == "active"  # not repaired


# --- isolation, degradation, restart, read-only ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_isolation_with_semantically_identical_content(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = world.other_scope()
    text_value = f"Conteúdo idêntico {uuid4().hex}"
    mine = await world.memory(text_value, cosine=0.9)
    theirs_mem = await world.memory(text_value, scope=other, cosine=0.9)
    theirs_ep = await world.episode(text_value, context_key="main", scope=other, cosine=0.9)
    theirs_cp = await world.checkpoint(scope=other)
    app = recall_app(world.factory, monkeypatch, VectorEmbeddingClient(dict(world.client.vectors)))
    data = await recall(app, world.scope, include_evidence=True)
    rendered = json.dumps(data)
    assert str(mine) in memory_ids(data)
    for foreign in (theirs_mem, theirs_ep, theirs_cp.checkpoint_id):
        assert str(foreign) not in rendered
    assert other not in rendered


@pytest.mark.integration
@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_deterministic_bounded_fallback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    cp = await world.checkpoint()
    newer_mem = await world.memory(f"Memória mais nova {uuid4().hex}")
    older = await world.episode(
        f"Antigo {uuid4().hex}", context_key="main", occurred_at="2026-11-01T10:00:00Z"
    )
    newer = await world.episode(
        f"Recente {uuid4().hex}", context_key=None, occurred_at="2026-11-15T10:00:00Z"
    )
    client = FailingEmbeddingClient()
    app = recall_app(world.factory, monkeypatch, client)

    data = await recall(app, world.scope, limits={"memories": 1})

    assert client.calls == 1  # one attempt; the fallback never calls the provider again
    assert data["warnings"] == ["PARTIAL_CONTEXT"]
    assert data["degraded_sources"] == ["memories", "episodes"]
    assert data["current_checkpoint"]["checkpoint_id"] == str(cp.checkpoint_id)
    assert data["continuity_available"] is True
    assert memory_ids(data) == [str(newer_mem)]  # created_at DESC, bounded
    assert episode_ids(data) == [str(newer), str(older)]  # occurred_at DESC
    assert all(item["relevance"] is None for item in data["episodes"] + data["current_memories"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_context_after_restart(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = await world.checkpoint()
    await world.episode(f"Persistente {uuid4().hex}", context_key="main", cosine=0.7)
    vectors = dict(world.client.vectors)
    first = await recall(
        recall_app(world.factory, monkeypatch, VectorEmbeddingClient(vectors)), world.scope
    )

    engine = create_async_engine_from_settings(load_settings())  # a brand-new process boundary
    try:
        fresh_factory = create_session_factory(engine)
        second = await recall(
            recall_app(fresh_factory, monkeypatch, VectorEmbeddingClient(vectors)), world.scope
        )
    finally:
        await dispose_async_engine(engine)

    assert second["current_checkpoint"]["checkpoint_id"] == str(cp.checkpoint_id)
    assert (memory_ids(second), episode_ids(second)) == (memory_ids(first), episode_ids(first))
    assert second["current_checkpoint"] == first["current_checkpoint"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_is_read_only(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await world.checkpoint()
    await world.episode(
        f"Com evidência {uuid4().hex}",
        context_key="main",
        cosine=0.8,
        sup=support("any_sufficient", ref("session_entry", world.seeds.session_entry)),
    )
    before = await row_counts(world.factory)
    for client in (VectorEmbeddingClient(dict(world.client.vectors)), FailingEmbeddingClient()):
        app = recall_app(world.factory, monkeypatch, client)
        for intent in ("resume", "context"):
            await recall(app, world.scope, intent=intent, include_evidence=True)
    assert await row_counts(world.factory) == before
