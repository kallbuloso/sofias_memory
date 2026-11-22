"""Unit tests for SM-CTX-005 propagation primitives (handoff 13 SS 66-68/93/136):
Episode rank order, the global Evidence share-lock order, the tombstone scrub
shape, safe counters and the Knowledge integration error mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import (
    EpisodeLifecycle,
    EvidenceGraphCorruptionError,
    EvidenceKey,
    EvidenceSourceType,
    episode_ranks,
)
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.services import contextual_evidence_invalidator as inv
from sofias_memory.services import forget as forget_service
from sofias_memory.services.contextual_evidence import EvidenceResolver
from sofias_memory.services.contextual_evidence_invalidator import (
    PROPAGATED_INVALIDATION_REASON,
    PropagationCounts,
    scrub_episode,
)

T = EvidenceSourceType


def ids(n: int) -> list[UUID]:
    return sorted(uuid4() for _ in range(n))


# --- episode_ranks -------------------------------------------------------------------------


def test_rank_is_longest_path_depth() -> None:
    a, b, c, d = ids(4)
    parents = {b: [a], c: [a, b], d: [c]}
    assert episode_ranks([d, a], parents) == {a: 0, b: 1, c: 2, d: 3}


def test_dependent_always_ranks_strictly_above_each_parent() -> None:
    nodes = ids(30)
    parents = {nodes[i]: nodes[max(0, i - 3) : i] for i in range(1, 30)}
    ranks = episode_ranks(nodes, parents)
    for child, ps in parents.items():
        assert all(ranks[child] > ranks[p] for p in ps)


@pytest.mark.parametrize("shape", ["self", "pair", "triangle"])
def test_cycles_fail_closed(shape: str) -> None:
    a, b, c = ids(3)
    graphs = {"self": {a: [a]}, "pair": {a: [b], b: [a]}, "triangle": {a: [b], b: [c], c: [a]}}
    with pytest.raises(EvidenceGraphCorruptionError):
        episode_ranks([a], graphs[shape])


def test_deep_chain_is_iterative() -> None:
    nodes = [uuid4() for _ in range(20_000)]
    parents = {nodes[i]: [nodes[i - 1]] for i in range(1, len(nodes))}
    assert episode_ranks([nodes[-1]], parents)[nodes[-1]] == len(nodes) - 1


# --- global share-lock order (Critical Gate 2) -----------------------------------------------


class RecordingRepo:
    def __init__(self, parents: dict[UUID, list[UUID]]) -> None:
        self.calls: list[tuple[str, list[UUID], bool]] = []
        self._parents = parents

    async def episode_parent_edges(self, episode_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        return self._parents

    def _record(self, name: str, values: Any, lock: bool) -> None:
        self.calls.append((name, list(values), lock))

    async def session_entries(self, values: Any, *, lock: bool) -> set[UUID]:
        self._record("session_entry", values, lock)
        return set(values)

    async def memory_items(self, values: Any, *, lock: bool) -> dict[UUID, Any]:
        self._record("memory_item", values, lock)
        return {}

    async def episodes(self, values: Any, *, lock: bool) -> dict[UUID, Any]:
        self._record("episode", values, lock)
        return {}

    async def sources(self, values: Any, *, lock: bool) -> dict[UUID, Any]:
        self._record("source", values, lock)
        return {}

    async def documents(self, values: Any, *, lock: bool) -> dict[UUID, Any]:
        self._record("document", values, lock)
        return {}

    async def chunks(self, values: Any, *, lock: bool) -> dict[UUID, Any]:
        self._record("chunk", values, lock)
        return {}


@pytest.mark.asyncio
async def test_locked_resolution_follows_forget_order_and_episode_rank() -> None:
    a, b = ids(2)  # a < b by id, but a depends on b -> rank(b) < rank(a)
    repo = RecordingRepo({a: [b]})
    resolver = EvidenceResolver(cast(Any, SimpleNamespace(contextual_evidence=repo)))
    keys = [
        EvidenceKey(T.EPISODE, a),
        EvidenceKey(T.CHUNK, uuid4()),
        EvidenceKey(T.SOURCE, uuid4()),
        EvidenceKey(T.EPISODE, b),
        EvidenceKey(T.DOCUMENT, uuid4()),
        EvidenceKey(T.MEMORY_ITEM, uuid4()),
        EvidenceKey(T.SESSION_ENTRY, uuid4()),
    ]
    await resolver.resolve(keys, lock=True)
    assert [c[0] for c in repo.calls] == [
        "session_entry",
        "memory_item",
        "source",
        "document",
        "chunk",
        "episode",
        "episode",
    ]
    assert [c[1] for c in repo.calls if c[0] == "episode"] == [[b], [a]]
    assert all(c[2] for c in repo.calls)


@pytest.mark.asyncio
async def test_unlocked_resolution_never_computes_ranks() -> None:
    a, b = ids(2)

    class NoRanks(RecordingRepo):
        async def episode_parent_edges(self, episode_ids: list[UUID]) -> dict[UUID, list[UUID]]:
            raise AssertionError("reads must not rank")

    repo = NoRanks({})
    resolver = EvidenceResolver(cast(Any, SimpleNamespace(contextual_evidence=repo)))
    await resolver.resolve([EvidenceKey(T.EPISODE, a), EvidenceKey(T.EPISODE, b)], lock=False)
    assert repo.calls == [("episode", [a, b], False)]


@pytest.mark.asyncio
async def test_locked_resolution_fails_closed_on_corrupt_episode_cycle() -> None:
    a, b = ids(2)
    resolver = EvidenceResolver(
        cast(Any, SimpleNamespace(contextual_evidence=RecordingRepo({a: [b], b: [a]})))
    )
    with pytest.raises(Exception) as caught:
        await resolver.resolve([EvidenceKey(T.EPISODE, a), EvidenceKey(T.EPISODE, b)], lock=True)
    assert getattr(caught.value, "code", None) is not None
    assert caught.value.code.value == "INTERNAL_ERROR"  # type: ignore[attr-defined]


# --- scrub shape and counters ----------------------------------------------------------------


def test_scrub_episode_produces_the_direct_forget_tombstone_shape() -> None:
    episode = SimpleNamespace(
        scope="project:x",
        context_key="k",
        kind="decision",
        summary="s",
        situation="si",
        reason="r",
        outcome="o",
        occurred_at=datetime.now(UTC),
        embedding=[0.1],
        confidence=0.5,
        invalidation_reason="why",
        replacement_episode_id=uuid4(),
        support_mode="all_required",
        lifecycle=EpisodeLifecycle.INVALIDATED,
        forgotten_at=None,
    )
    provenance = SimpleNamespace(conversation_ref="c", turn_ref="t", source_system="sys")
    now = datetime.now(UTC)
    scrub_episode(cast(Any, episode), cast(Any, provenance), now=now)
    for field in (
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
        "support_mode",
    ):
        assert getattr(episode, field) is None, field
    assert episode.lifecycle is EpisodeLifecycle.FORGOTTEN and episode.forgotten_at == now
    assert provenance.conversation_ref is None and provenance.turn_ref is None
    assert provenance.source_system == "sys"


def test_counters_are_safe_integers_only() -> None:
    fields = PropagationCounts(1, 2, 3, 4).as_log_fields()
    assert fields == {
        "evidence_sources_processed": 1,
        "episodes_forgotten": 2,
        "statements_removed": 3,
        "checkpoints_invalidated": 4,
    }
    assert "evidence" in PROPAGATED_INVALIDATION_REASON and len(PROPAGATED_INVALIDATION_REASON) < 60


# --- Knowledge integration mapping -------------------------------------------------------------


class SeedRepo:
    def __init__(self, seeds: list[EvidenceKey], dataset_sources: list[UUID]) -> None:
        self.seed_calls: list[list[UUID]] = []
        self._seeds = seeds
        self._dataset_sources = dataset_sources

    async def knowledge_seed_keys(self, source_ids: list[UUID]) -> list[EvidenceKey]:
        self.seed_calls.append(list(source_ids))
        return self._seeds

    async def source_ids_for_dataset(self, dataset_id: UUID) -> list[UUID]:
        return self._dataset_sources


@pytest.mark.asyncio
async def test_knowledge_loss_includes_deleted_dataset_sources_and_skips_without_seeds() -> None:
    live, deleted = ids(2)
    repo = SeedRepo([], [live, deleted])
    uow = SimpleNamespace(contextual_evidence=repo)
    await forget_service.propagate_contextual_knowledge_loss(
        cast(Any, uow), source_ids=[live], dataset_id=uuid4()
    )
    assert repo.seed_calls == [sorted([live, deleted])]


@pytest.mark.asyncio
async def test_knowledge_corruption_becomes_a_sanitized_permanent_step_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def corrupt(uow: Any, source_ids: Any) -> PropagationCounts:
        raise EvidenceGraphCorruptionError("Episode Evidence graph contains a cycle.")

    monkeypatch.setattr(forget_service, "propagate_knowledge_evidence_loss", corrupt)
    uow = SimpleNamespace(contextual_evidence=SeedRepo([], []))
    with pytest.raises(PermanentPipelineStepError) as caught:
        await forget_service.propagate_contextual_knowledge_loss(
            cast(Any, uow), source_ids=[uuid4()]
        )
    assert caught.value.code == forget_service.FORGET_CONTEXTUAL_PROPAGATION_ERROR_CODE
    assert "cycle" not in str(caught.value.message).lower()


def test_invalidator_module_never_imports_llm_or_neo4j() -> None:
    source = open(inv.__file__, encoding="utf-8").read()  # noqa: PTH123, SIM115
    for forbidden in (
        "neo4j",
        "Neo4j",
        "embedding_client",
        "openai",
        "graph_outbox",
        "PipelineRun",
    ):
        assert f"import {forbidden}" not in source and f"from {forbidden}" not in source
    assert "sofias_memory.infrastructure.neo4j" not in source
    assert "OpenAI" not in source


class _FakeOrig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlstate", "retryable"), [("40P01", True), ("40001", True), ("23505", False)]
)
async def test_knowledge_propagation_conflicts_are_retryable_only_when_transient(
    monkeypatch: pytest.MonkeyPatch, sqlstate: str, retryable: bool
) -> None:
    from sqlalchemy.exc import DBAPIError

    from sofias_memory.pipelines.errors import RetryablePipelineStepError

    async def conflict(uow: Any, source_ids: Any) -> PropagationCounts:
        raise DBAPIError("SELECT 1", {}, _FakeOrig(sqlstate))

    monkeypatch.setattr(forget_service, "propagate_knowledge_evidence_loss", conflict)
    uow = SimpleNamespace(contextual_evidence=SeedRepo([], []))
    if retryable:
        with pytest.raises(RetryablePipelineStepError) as caught:
            await forget_service.propagate_contextual_knowledge_loss(
                cast(Any, uow), source_ids=[uuid4()]
            )
        assert caught.value.code == forget_service.FORGET_CONTEXTUAL_PROPAGATION_CONFLICT_CODE
    else:
        with pytest.raises(DBAPIError):
            await forget_service.propagate_contextual_knowledge_loss(
                cast(Any, uow), source_ids=[uuid4()]
            )
