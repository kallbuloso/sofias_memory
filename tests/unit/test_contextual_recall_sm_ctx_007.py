"""SM-CTX-007 unit proof (handoff 15 SS 151-169, AC-007.x): explain/history
composition, the bounded Evidence trace, explicit-dataset Knowledge references
and the shared single embedding -- pure composition plus orchestration over a
fake unit of work. The real SQL eligibility is proven by the PostgreSQL suite."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import (
    DatasetStatus,
    EpisodeLifecycle,
    EvidenceKey,
    EvidenceSourceType,
    SupportMode,
)
from sofias_memory.infrastructure.postgres.repositories.chunks import RetrievedChunk
from sofias_memory.infrastructure.postgres.repositories.contextual_evidence import (
    KnowledgeLocation,
)
from sofias_memory.schemas.context_recall import ContextRecallLimits
from sofias_memory.schemas.contextual_evidence import SupportResult
from sofias_memory.services import contextual_evidence_trace as trace_module
from sofias_memory.services import contextual_knowledge_retriever as retriever_module
from sofias_memory.services import contextual_recall as module
from sofias_memory.services.contextual_evidence_trace import (
    EvidenceTrace,
    has_cycle,
    read_evidence_trace,
)
from sofias_memory.services.contextual_knowledge_retriever import (
    ContextualKnowledgeRetriever,
    DatasetSelection,
    KnowledgeHit,
)
from sofias_memory.services.contextual_recall import (
    ContextualRecallService,
    EpisodeCandidate,
    MemoryCandidate,
    SourceSnapshot,
    compose_bundle,
)
from tests.unit.test_contextual_recall_service import (
    NOW,
    A,
    CountingEmbeddingClient,
    F,
    U,
    checkpoint,
    episode,
    evidence,
    make_settings,
    memory,
    request,
    stmt,
    sup,
)

EP = EvidenceSourceType.EPISODE
CH = EvidenceSourceType.CHUNK


def snap(**fields: Any) -> SourceSnapshot:
    base: dict[str, Any] = {"active_checkpoints": [], "memories": [], "episodes": []}
    base.update(fields)
    return SourceSnapshot(**base)


def ref_to(source_type: EvidenceSourceType, source_id: UUID, availability: Any = A) -> Any:
    item = evidence(source_type, availability)
    return item.model_copy(update={"source_id": source_id})


def warnings(bundle: Any) -> list[str]:
    return [w.value for w in bundle.warnings]


def compose(intent: str, snapshot: SourceSnapshot, **overrides: Any) -> Any:
    degraded = overrides.pop("semantic_degraded", False)
    limits = overrides.pop("limits", ContextRecallLimits())
    extra = {k: v for k, v in overrides.items() if k.startswith("max_")}
    req = request(intent=intent, **{k: v for k, v in overrides.items() if k not in extra})
    return compose_bundle(req, limits, snapshot, semantic_degraded=degraded, **extra)


# --- SS 151 intent matrix -----------------------------------------------------------------


@pytest.mark.parametrize("intent", ["resume", "context", "explain", "history"])
def test_intent_matrix(intent: str) -> None:
    hist = checkpoint(revision=1, lifecycle="superseded")
    s = snap(
        active_checkpoints=[checkpoint(revision=2)],
        memories=[MemoryCandidate(memory(), 0.9)],
        episodes=[EpisodeCandidate(episode(), 0.8)],
        historical_checkpoints=[hist],
        trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False),
    )
    bundle = compose(intent, s)
    assert bundle.intent.value == intent
    assert bundle.current_checkpoint is not None  # structural, every intent
    assert len(bundle.episodes) == 1
    if intent == "history":
        assert bundle.current_memories == []  # current truth != timeline
        assert [c.checkpoint_id for c in bundle.historical_checkpoints] == [hist.checkpoint_id]
        assert bundle.historical_checkpoints[0].result_class == "historical_checkpoint"
    else:
        assert len(bundle.current_memories) == 1
        assert bundle.historical_checkpoints == []  # lineage is never explanation
    assert bundle.knowledge_references == []  # no datasets, no Knowledge
    assert warnings(bundle) == [] and bundle.degraded_sources == []


# --- SS 152/83/170 explain Evidence edges ---------------------------------------------------


class TraceUow:
    """Episodes -> (support_mode, support) for the trace; counts reads."""

    def __init__(self, graph: dict[UUID, SupportResult | None]) -> None:
        self.graph = graph
        self.reads: list[list[UUID]] = []
        uow = self

        class Episodes:
            async def support_modes(self, ids: Sequence[UUID]) -> dict[UUID, Any]:
                uow.reads.append(list(ids))
                return {
                    i: (uow.graph[i].mode if uow.graph.get(i) else None)
                    for i in ids
                    if i in uow.graph
                }

        self.episodes = Episodes()


@pytest.fixture()
def trace_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hydrate(uow: TraceUow, modes: dict[UUID, Any]) -> dict[UUID, Any]:
        return {owner: (uow.graph.get(owner) if mode else None) for owner, mode in modes.items()}

    monkeypatch.setattr(trace_module, "hydrate_episode_support", hydrate)


@pytest.mark.asyncio
@pytest.mark.usefixtures("trace_hydration")
async def test_statement_episode_chunk_chain_preserves_every_edge() -> None:
    a, chunk = uuid4(), uuid4()
    s_support = sup(SupportMode.ALL_REQUIRED, ref_to(EP, a))
    a_support = sup(SupportMode.ALL_REQUIRED, ref_to(CH, chunk))
    uow = TraceUow({a: a_support})
    trace = await read_evidence_trace(uow, [s_support])  # type: ignore[arg-type]
    edges = trace.edges()
    assert [(e.owner_type, e.owner_id, e.source_type, e.source_id) for e in edges] == [
        ("episode", a, CH, chunk)
    ]
    assert edges[0].support_mode is SupportMode.ALL_REQUIRED
    assert edges[0].availability.value == "available"
    assert trace.cut == frozenset() and trace.cycle is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("trace_hydration")
async def test_episode_chain_b_a_memory_leaf_and_unavailable_stop() -> None:
    b, a, mem, invalid = uuid4(), uuid4(), uuid4(), uuid4()
    graph: dict[UUID, SupportResult | None] = {
        b: sup(SupportMode.ALL_REQUIRED, ref_to(EP, a)),
        a: sup(SupportMode.ANY_SUFFICIENT, ref_to(EvidenceSourceType.MEMORY_ITEM, mem)),
        invalid: sup(SupportMode.ALL_REQUIRED, ref_to(CH, uuid4())),
    }
    uow = TraceUow(graph)
    root = sup(SupportMode.ALL_REQUIRED, ref_to(EP, b), ref_to(EP, invalid, U))
    trace = await read_evidence_trace(uow, [root])  # type: ignore[arg-type]
    assert set(trace.supports) == {a, b}  # the UNAVAILABLE Episode is never traversed
    assert [(e.owner_id, e.source_id) for e in trace.edges()] == sorted(
        [(a, mem), (b, a)], key=lambda p: str(p[0])
    )
    assert uow.reads == [[b], [a]]  # level by level, MemoryItem is a leaf


@pytest.mark.asyncio
@pytest.mark.usefixtures("trace_hydration")
async def test_trace_depth_and_breadth_bounds_are_recorded_never_complete() -> None:
    chain = [uuid4() for _ in range(5)]
    graph: dict[UUID, SupportResult | None] = {
        chain[i]: sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[i + 1])) for i in range(4)
    }
    graph[chain[4]] = None
    root = sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[0]))
    deep = await read_evidence_trace(TraceUow(graph), [root], max_depth=2)  # type: ignore[arg-type]
    assert set(deep.supports) == {chain[0], chain[1]} and deep.cut == frozenset({chain[2]})
    wide_ids = [uuid4() for _ in range(6)]
    wide_root = sup(SupportMode.ANY_SUFFICIENT, *(ref_to(EP, i) for i in wide_ids))
    wide = await read_evidence_trace(  # type: ignore[arg-type]
        TraceUow(dict.fromkeys(wide_ids)), [wide_root], max_episodes=4
    )
    assert len(wide.supports) == 4 and len(wide.cut) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("trace_hydration")
async def test_corrupt_cycle_terminates_and_is_reported() -> None:
    x, y = uuid4(), uuid4()
    graph: dict[UUID, SupportResult | None] = {
        x: sup(SupportMode.ALL_REQUIRED, ref_to(EP, y)),
        y: sup(SupportMode.ALL_REQUIRED, ref_to(EP, x)),
    }
    trace = await read_evidence_trace(  # type: ignore[arg-type]
        TraceUow(graph), [sup(SupportMode.ALL_REQUIRED, ref_to(EP, x))]
    )
    assert set(trace.supports) == {x, y} and trace.cycle is True
    assert has_cycle({x: graph[x], y: None}) is False


def owners_of(bundle: Any, source_id: UUID) -> list[tuple[str, UUID, str]]:
    for item in bundle.evidence:
        if item.source_id == source_id:
            return [(o.owner_type, o.owner_id, o.support_mode.value) for o in item.owners]
    raise AssertionError(f"{source_id} not in evidence")


def chain_snapshot(*, returned: str = "b") -> tuple[SourceSnapshot, dict[str, UUID]]:
    """S -> B -> A -> K with only B (or nothing) returned in episodes[]."""

    b, a, k = uuid4(), uuid4(), uuid4()
    a_support = sup(SupportMode.ANY_SUFFICIENT, ref_to(CH, k))
    b_support = sup(SupportMode.ALL_REQUIRED, ref_to(EP, a))
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, b)))
    cp = checkpoint(current_state=cs)
    episodes = [EpisodeCandidate(episode(episode_id=b, support=b_support), 0.9)] if returned else []
    trace = EvidenceTrace(supports={b: b_support, a: a_support}, cut=frozenset(), cycle=False)
    ids = {"s": cs.statement_id, "b": b, "a": a, "k": k}
    return snap(active_checkpoints=[cp], episodes=episodes, trace=trace), ids


def test_multi_hop_trace_preserves_every_edge_with_intermediate_episode_not_returned() -> None:
    s, ids = chain_snapshot()
    bundle = compose("explain", s, include_evidence=True)
    assert [e.episode.episode_id for e in bundle.episodes] == [ids["b"]]  # A is not returned
    assert owners_of(bundle, ids["b"]) == [("context_statement", ids["s"], "all_required")]
    assert owners_of(bundle, ids["a"]) == [("episode", ids["b"], "all_required")]
    assert owners_of(bundle, ids["k"]) == [("episode", ids["a"], "any_sufficient")]
    assert warnings(bundle) == []  # complete trace: absence of A from episodes[] is not partial
    assert "summary" not in bundle.evidence[0].model_dump()  # identity only, never A's payload


def test_trace_from_checkpoint_only_still_reaches_the_leaf() -> None:
    s, ids = chain_snapshot(returned="")
    bundle = compose("explain", s, include_evidence=True)
    assert bundle.episodes == []
    assert [e.source_id for e in bundle.evidence] == sorted(
        [ids["b"], ids["a"], ids["k"]],
        key=lambda i: ("chunk" if i == ids["k"] else "episode", str(i)),
    )
    assert warnings(bundle) == []


def test_include_evidence_false_keeps_the_previous_output_contract() -> None:
    s, ids = chain_snapshot()
    bundle = compose("explain", s, include_evidence=False)
    assert bundle.evidence == [] and warnings(bundle) == []
    assert bundle.current_checkpoint is not None
    assert bundle.current_checkpoint.current_state is not None
    cs_support = bundle.current_checkpoint.current_state.support
    assert cs_support is not None and [e.source_id for e in cs_support.evidence] == [ids["b"]]
    assert "owners" not in cs_support.evidence[0].model_dump()  # per-resource refs unchanged


def test_direct_and_multiple_owners_keep_each_support_mode_deterministically() -> None:
    shared = evidence(EvidenceSourceType.SESSION_ENTRY, A)
    e1 = episode(support=sup(SupportMode.ALL_REQUIRED, shared))
    e2 = episode(support=sup(SupportMode.ANY_SUFFICIENT, shared, evidence(CH, A)))
    cs = stmt("estado", sup(SupportMode.ANY_SUFFICIENT, shared))
    for intent in ("resume", "explain"):
        bundles = []
        for order in ([e1, e2], [e2, e1]):
            s = snap(
                active_checkpoints=[checkpoint(current_state=cs)],
                episodes=[EpisodeCandidate(e, 0.5) for e in order],
                trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False),
            )
            bundles.append(compose(intent, s, include_evidence=True))
        assert bundles[0].evidence == bundles[1].evidence  # order-independent
        expected = sorted(
            [
                ("context_statement", cs.statement_id, "any_sufficient"),
                ("episode", e1.episode_id, "all_required"),
                ("episode", e2.episode_id, "any_sufficient"),
            ],
            key=lambda o: (o[0], str(o[1])),
        )
        assert owners_of(bundles[0], shared.source_id) == expected
        assert [e.source_id for e in bundles[0].evidence].count(shared.source_id) == 1


def test_identity_cap_is_partial_and_keeps_a_coherent_prefix() -> None:
    s, ids = chain_snapshot()
    bundle = compose("explain", s, include_evidence=True, limits=ContextRecallLimits(evidence=1))
    assert [e.source_id for e in bundle.evidence] == [ids["b"]]  # root edge first
    assert warnings(bundle) == ["PARTIAL_CONTEXT"] and bundle.degraded_sources == []
    two = compose("explain", s, include_evidence=True, limits=ContextRecallLimits(evidence=2))
    assert {e.source_id for e in two.evidence} == {ids["b"], ids["a"]}  # K never orphaned
    assert "PARTIAL_CONTEXT" in warnings(two)


def test_owner_edge_cap_is_partial_for_every_intent() -> None:
    shared = evidence(EvidenceSourceType.SESSION_ENTRY, A)
    eps = [
        EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, shared)), 0.5)
        for _ in range(3)
    ]
    trace = EvidenceTrace(supports={}, cut=frozenset(), cycle=False)
    items, partial = module.aggregate_evidence(
        None,
        [module.ContextRecallEpisode(episode=c.episode, relevance=None) for c in eps],
        10,
        max_owner_edges=2,
    )
    assert partial is True and len(items[0].owners) == 2
    returned = [module.ContextRecallEpisode(episode=c.episode, relevance=None) for c in eps]
    traced, partial = module.trace_evidence(None, returned, trace, 10, max_owner_edges=2)
    assert partial is True and len(traced[0].owners) == 2
    _, complete = module.trace_evidence(None, returned, trace, 10)
    assert complete is False


def test_resume_identity_limit_keeps_the_sm_ctx_006_semantics() -> None:
    refs3 = [evidence(EvidenceSourceType.SESSION_ENTRY, A) for _ in range(3)]
    s = snap(
        episodes=[EpisodeCandidate(episode(support=sup(SupportMode.ANY_SUFFICIENT, *refs3)), 0.5)]
    )
    bundle = compose("resume", s, include_evidence=True, limits=ContextRecallLimits(evidence=2))
    assert len(bundle.evidence) == 2 and "PARTIAL_CONTEXT" not in warnings(bundle)


@pytest.mark.parametrize("kind", ["cut", "cycle", "untraced"])
def test_traversal_bounds_and_corruption_are_partial(kind: str) -> None:
    s, ids = chain_snapshot()
    assert s.trace is not None
    if kind == "cut":
        trace = EvidenceTrace(
            supports={ids["b"]: s.trace.supports[ids["b"]]}, cut=frozenset({ids["a"]}), cycle=False
        )
    elif kind == "cycle":
        trace = EvidenceTrace(supports=s.trace.supports, cut=frozenset(), cycle=True)
    else:
        trace = EvidenceTrace(supports={}, cut=frozenset(), cycle=False)
    bundle = compose(
        "explain",
        snap(active_checkpoints=s.active_checkpoints, episodes=s.episodes, trace=trace),
        include_evidence=True,
    )
    assert warnings(bundle) == ["PARTIAL_CONTEXT"] and bundle.degraded_sources == []
    shown = [e.source_id for e in bundle.evidence]
    if kind == "cycle":
        assert len(shown) == len(set(shown))  # bounded, persisted links only, flagged partial
    else:
        assert ids["k"] not in shown  # an unexpanded Episode's support is never fabricated


@pytest.mark.asyncio
@pytest.mark.usefixtures("trace_hydration")
async def test_depth_and_breadth_caps_from_the_real_traversal_signal_partial() -> None:
    chain = [uuid4() for _ in range(4)]
    graph: dict[UUID, SupportResult | None] = {
        chain[i]: sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[i + 1])) for i in range(3)
    }
    graph[chain[3]] = None
    root = stmt("raiz", sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[0])))
    cp = checkpoint(current_state=root)
    deep = await read_evidence_trace(TraceUow(graph), [root.support], max_depth=2)  # type: ignore[arg-type]
    bundle = compose("explain", snap(active_checkpoints=[cp], trace=deep), include_evidence=True)
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]
    full = await read_evidence_trace(TraceUow(graph), [root.support])  # type: ignore[arg-type]
    assert (
        warnings(
            compose("explain", snap(active_checkpoints=[cp], trace=full), include_evidence=True)
        )
        == []
    )
    wide_ids = [uuid4() for _ in range(5)]
    wide = stmt("larga", sup(SupportMode.ANY_SUFFICIENT, *(ref_to(EP, i) for i in wide_ids)))
    narrow = await read_evidence_trace(  # type: ignore[arg-type]
        TraceUow(dict.fromkeys(wide_ids)), [wide.support], max_episodes=3
    )
    cp2 = checkpoint(current_state=wide)
    assert warnings(
        compose("explain", snap(active_checkpoints=[cp2], trace=narrow), include_evidence=True)
    ) == ["PARTIAL_CONTEXT"]


def test_declared_support_without_links_fails_closed_in_explain() -> None:
    declared = EpisodeCandidate(episode(support=None), 0.99, support_declared=True)
    bundle = compose(
        "explain",
        snap(episodes=[declared], trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False)),
        include_evidence=True,
    )
    assert bundle.episodes == [] and bundle.evidence == []
    assert set(warnings(bundle)) >= {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}


def test_many_owners_stay_inside_the_byte_budget() -> None:
    shared = [evidence(EvidenceSourceType.SESSION_ENTRY, A) for _ in range(100)]
    eps = [
        EpisodeCandidate(episode(support=sup(SupportMode.ANY_SUFFICIENT, *shared[i : i + 40])), 0.5)
        for i in range(0, 60)
    ]
    bundle = compose(
        "explain",
        snap(episodes=eps, trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False)),
        include_evidence=True,
        limits=ContextRecallLimits(episodes=50, evidence=100),
    )
    assert module.serialized_bundle_bytes(bundle) <= module.CONTEXT_RECALL_MAX_BUNDLE_BYTES
    assert sum(len(e.owners) for e in bundle.evidence) <= module.CONTEXT_RECALL_MAX_EVIDENCE_OWNERS
    assert "PARTIAL_CONTEXT" in warnings(bundle)  # owner-link cap reached, never silent


# --- SS 153/154 lineage is not causality / no Evidence ------------------------------------


def test_lineage_without_evidence_is_never_causal() -> None:
    rev1 = checkpoint(revision=1, lifecycle="superseded")
    rev2 = checkpoint(revision=2)
    s = snap(
        active_checkpoints=[rev2],
        historical_checkpoints=[rev1],
        trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False),
    )
    explained = compose("explain", s, include_evidence=True)
    assert explained.historical_checkpoints == []  # rev1 is not presented as a cause
    assert explained.evidence == [] and warnings(explained) == []
    assert explained.degraded_sources == []
    history = compose("history", s, include_evidence=True)
    assert [c.revision for c in history.historical_checkpoints] == [1]
    assert history.evidence == []  # chronology never becomes Evidence


def test_explain_without_declared_support_fabricates_nothing() -> None:
    s = snap(
        active_checkpoints=[checkpoint()],
        episodes=[EpisodeCandidate(episode(), 0.7)],
        trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False),
    )
    bundle = compose("explain", s, include_evidence=True)
    assert bundle.current_checkpoint is not None
    assert bundle.current_checkpoint.current_state is not None
    assert bundle.current_checkpoint.current_state.support is None
    assert bundle.episodes[0].episode.support is None
    assert bundle.evidence == [] and warnings(bundle) == [] and bundle.degraded_sources == []


# --- SS 155-159 history -------------------------------------------------------------------


def test_history_checkpoints_keep_repository_revision_order_and_current_separate() -> None:
    rev1 = checkpoint(revision=1, lifecycle="superseded")
    rev2 = checkpoint(revision=2, lifecycle="invalidated", invalidated_at=NOW)
    rev3 = checkpoint(revision=3)
    bundle = compose(
        "history", snap(active_checkpoints=[rev3], historical_checkpoints=[rev2, rev1])
    )
    assert [c.checkpoint_id for c in bundle.historical_checkpoints] == [
        rev2.checkpoint_id,
        rev1.checkpoint_id,
    ]
    assert [c.lifecycle.value for c in bundle.historical_checkpoints] == [
        "invalidated",
        "superseded",
    ]
    assert bundle.current_checkpoint is not None
    assert bundle.current_checkpoint.checkpoint_id == rev3.checkpoint_id
    assert bundle.current_checkpoint.result_class == "current_checkpoint"
    limited = compose(
        "history",
        snap(historical_checkpoints=[rev2, rev1]),
        limits=ContextRecallLimits(historical_checkpoints=1),
    )
    assert [c.revision for c in limited.historical_checkpoints] == [2]


def test_history_statement_eligibility_never_exposes_post_forget_content() -> None:
    forgotten = sup(SupportMode.ALL_REQUIRED, evidence(EP, F))
    invalidated = sup(SupportMode.ALL_REQUIRED, evidence(EP, U))
    keep_unsupported = stmt("existiu, suporte invalidado", invalidated)
    rev = checkpoint(
        revision=1,
        lifecycle="superseded",
        current_state=stmt("deveria ter sido scrubbed", forgotten),
        pending_work=[keep_unsupported],
    )
    bundle = compose("history", snap(historical_checkpoints=[rev]))
    (hist,) = bundle.historical_checkpoints
    assert hist.current_state is None  # invariant violation: never exposed
    assert [s.statement_id for s in hist.pending_work] == [keep_unsupported.statement_id]
    assert hist.pending_work[0].support is not None
    assert hist.pending_work[0].support.evidence[0].availability.value == "unavailable"
    assert warnings(bundle) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT", "EVIDENCE_UNAVAILABLE"]


def test_history_episodes_active_and_invalidated_chronological_with_lifecycle() -> None:
    old_active = episode(minutes=30)
    new_invalid = episode(minutes=10).model_copy(
        update={"lifecycle": EpisodeLifecycle.INVALIDATED, "invalidated_at": NOW}
    )
    tie_ids = sorted([uuid4(), uuid4()])
    tie_b, tie_a = (
        episode(minutes=20, episode_id=tie_ids[1]),
        episode(minutes=20, episode_id=tie_ids[0]),
    )
    candidates = [  # semantic (relevance) order
        EpisodeCandidate(old_active, 0.99),
        EpisodeCandidate(tie_b, 0.9),
        EpisodeCandidate(new_invalid, 0.8),
        EpisodeCandidate(tie_a, 0.7),
    ]
    bundle = compose("history", snap(episodes=candidates))
    assert [e.episode.episode_id for e in bundle.episodes] == [
        new_invalid.episode_id,  # occurred_at DESC
        tie_ids[0],  # equal occurred_at: episode_id ASC
        tie_ids[1],
        old_active.episode_id,
    ]
    assert [e.relevance for e in bundle.episodes] == [0.8, 0.7, 0.9, 0.99]  # never re-scored
    assert {e.episode.lifecycle.value for e in bundle.episodes} == {"active", "invalidated"}
    assert all(e.result_class == "historical_episode" for e in bundle.episodes)
    top2 = compose("history", snap(episodes=candidates), limits=ContextRecallLimits(episodes=2))
    # candidate selection by relevance first, then chronological presentation
    assert [e.episode.episode_id for e in top2.episodes] == [
        tie_b.episode_id,
        old_active.episode_id,
    ]


def test_history_shows_ordinarily_unsupported_episode_but_never_forget_violations() -> None:
    shown = EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, evidence(EP, U))), 0.5)
    violating = EpisodeCandidate(
        episode(support=sup(SupportMode.ALL_REQUIRED, evidence(CH, F))), 0.9
    )
    bundle = compose("history", snap(episodes=[violating, shown]))
    assert [e.episode.episode_id for e in bundle.episodes] == [shown.episode.episode_id]
    assert set(warnings(bundle)) == {
        "NO_CURRENT_CHECKPOINT",
        "PARTIAL_CONTEXT",
        "EVIDENCE_UNAVAILABLE",
    }
    only_shown = compose("history", snap(episodes=[shown]))
    assert warnings(only_shown) == ["NO_CURRENT_CHECKPOINT", "EVIDENCE_UNAVAILABLE"]
    # The same unsupported Episode is never usable context for resume/explain.
    assert compose("explain", snap(episodes=[shown])).episodes == []


def test_history_never_injects_cognitive_memory() -> None:
    s = snap(memories=[MemoryCandidate(memory(), 0.99)])
    assert compose("history", s).current_memories == []
    assert compose("history", s, semantic_degraded=True).degraded_sources[0].value == "episodes"


# --- SS 166-168/114-118 Knowledge references ------------------------------------------------

DS_A = DatasetSelection(dataset_id=uuid4(), slug="docs")
DS_B_ID = uuid4()


def hit(rank: int, *, dataset_id: UUID = DS_A.dataset_id, chunk_id: UUID | None = None) -> Any:
    return KnowledgeHit(
        dataset_id=dataset_id,
        source_id=uuid4(),
        document_id=uuid4(),
        chunk_id=chunk_id or uuid4(),
        rank=rank,
        relevance=1 / (60 + rank),
    )


def test_knowledge_hits_are_references_not_evidence_and_never_text() -> None:
    hits = [hit(1), hit(2)]
    s = snap(datasets=[DS_A], knowledge_hits=hits, episodes=[EpisodeCandidate(episode(), 0.5)])
    bundle = compose("context", s, include_evidence=True)
    refs = bundle.knowledge_references
    assert [r.chunk_id for r in refs] == [h.chunk_id for h in hits]
    assert [r.rank for r in refs] == [1, 2]
    assert all(r.result_class == "knowledge_reference" and not r.evidence_backed for r in refs)
    assert all(r.dataset_slug == "docs" for r in refs)
    assert bundle.evidence == []  # a semantic hit never becomes Evidence
    assert "text" not in refs[0].model_dump()


def test_evidence_backed_reference_of_a_selected_dataset_without_retrieval() -> None:
    chunk, document = uuid4(), uuid4()
    ep = episode(support=sup(SupportMode.ALL_REQUIRED, ref_to(CH, chunk)))
    location = KnowledgeLocation(DS_A.dataset_id, uuid4(), document, chunk)
    s = snap(
        datasets=[DS_A],
        episodes=[EpisodeCandidate(ep, None)],
        knowledge_locations={EvidenceKey(CH, chunk): location},
    )
    bundle = compose("resume", s, semantic_degraded=True)
    (ref,) = bundle.knowledge_references
    assert (ref.chunk_id, ref.document_id, ref.evidence_backed) == (chunk, document, True)
    assert ref.rank is None and ref.relevance is None  # never fabricated
    assert [s.value for s in bundle.degraded_sources] == ["memories", "episodes", "knowledge"]
    assert warnings(bundle).count("PARTIAL_CONTEXT") == 1  # one cause, one warning


def test_evidence_of_another_dataset_stays_evidence_only() -> None:
    chunk = uuid4()
    ep = episode(support=sup(SupportMode.ALL_REQUIRED, ref_to(CH, chunk)))
    s = snap(
        datasets=[DS_A],
        episodes=[EpisodeCandidate(ep, 0.5)],
        knowledge_locations={},  # the reader keeps only selected datasets
    )
    bundle = compose("explain", s, include_evidence=True)
    assert bundle.knowledge_references == []
    assert [e.source_id for e in bundle.evidence] == [chunk]
    other = module.knowledge_references(
        [],
        [ep.support],
        {EvidenceKey(CH, chunk): KnowledgeLocation(DS_B_ID, uuid4(), uuid4(), chunk)},
        [DS_A],
        8,
    )
    assert other == []  # B's chunk is never promoted into A's selection


def test_one_reference_for_a_hit_that_is_also_evidence_and_unavailable_never_promoted() -> None:
    shared, gone = uuid4(), uuid4()
    support = sup(SupportMode.ANY_SUFFICIENT, ref_to(CH, shared), ref_to(CH, gone, F))
    ep = episode(support=support)
    locations = {
        EvidenceKey(CH, shared): KnowledgeLocation(DS_A.dataset_id, uuid4(), uuid4(), shared),
        EvidenceKey(CH, gone): KnowledgeLocation(DS_A.dataset_id, uuid4(), uuid4(), gone),
    }
    s = snap(
        datasets=[DS_A],
        knowledge_hits=[hit(1, chunk_id=shared)],
        episodes=[EpisodeCandidate(ep, 0.5)],
        knowledge_locations=locations,
    )
    bundle = compose("context", s, include_evidence=True)
    assert [(r.chunk_id, r.rank, r.evidence_backed) for r in bundle.knowledge_references] == [
        (shared, 1, True)
    ]
    assert {e.source_id for e in bundle.evidence} == {shared, gone}  # both roles kept


def test_include_evidence_false_keeps_refs_and_availability() -> None:
    support = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.SESSION_ENTRY, A))
    s = snap(
        episodes=[EpisodeCandidate(episode(support=support), 0.5)],
        trace=EvidenceTrace(supports={}, cut=frozenset(), cycle=False),
    )
    bundle = compose("explain", s, include_evidence=False)
    assert bundle.evidence == []
    assert bundle.episodes[0].episode.support == support


# --- SS 113 / R5 budget ----------------------------------------------------------------------


def test_budget_omits_history_and_knowledge_deterministically_without_degradation() -> None:
    big = [
        checkpoint(revision=r, lifecycle="superseded", objective=stmt("x" * 2000))
        for r in range(20, 0, -1)
    ]
    hits = [hit(i) for i in range(1, 40)]
    s = snap(datasets=[DS_A], historical_checkpoints=big, knowledge_hits=hits)
    full = compose(
        "history", s, limits=ContextRecallLimits(historical_checkpoints=20, knowledge=50)
    )
    assert len(full.historical_checkpoints) == 20 and len(full.knowledge_references) == 39
    bounded = compose(
        "history",
        s,
        limits=ContextRecallLimits(historical_checkpoints=20, knowledge=50),
        max_bundle_bytes=30_000,
    )
    assert len(module.serialized_bundle_bytes(bounded).__str__()) > 0
    assert module.serialized_bundle_bytes(bounded) <= 30_000
    kept = [c.revision for c in bounded.historical_checkpoints]
    assert kept == list(range(20, 20 - len(kept), -1))  # a prefix of the ranking
    assert len(bounded.historical_checkpoints) < 20
    assert "PARTIAL_CONTEXT" in warnings(bounded)
    assert bounded.degraded_sources == []  # truncation is not degradation
    again = compose(
        "history",
        s,
        limits=ContextRecallLimits(historical_checkpoints=20, knowledge=50),
        max_bundle_bytes=30_000,
    )
    assert again == bounded


def test_historical_checkpoint_over_statement_bound_is_omitted_whole() -> None:
    many = checkpoint(
        revision=2, lifecycle="superseded", pending_work=[stmt(f"p{i}") for i in range(70)]
    )
    small = checkpoint(revision=1, lifecycle="superseded")
    bundle = compose("history", snap(historical_checkpoints=[many, small]))
    assert [c.revision for c in bundle.historical_checkpoints] == [1]
    assert "PARTIAL_CONTEXT" in warnings(bundle)


# --- orchestration: SS 159-165 ---------------------------------------------------------------


class SpyRetriever(ContextualKnowledgeRetriever):
    def __init__(self, datasets: Sequence[DatasetSelection] = ()) -> None:
        super().__init__(rrf_k=60)
        self.datasets = list(datasets)
        self.resolved: list[list[str]] = []
        self.retrieved: list[dict[str, Any]] = []

    async def resolve_datasets(self, uow: Any, slugs: Sequence[str]) -> list[DatasetSelection]:
        self.resolved.append(list(slugs))
        return self.datasets

    async def retrieve(self, uow: Any, **kwargs: Any) -> list[KnowledgeHit]:
        self.retrieved.append(kwargs)
        return []


class Uow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

        def method(name: str, result: Any) -> Any:
            async def call(*args: Any, **kwargs: Any) -> Any:
                self.calls.append((name, kwargs))
                return result

            return staticmethod(call)

        self.context_checkpoints = type(
            "C",
            (),
            {"list_active": method("list_active", []), "list_history": method("list_history", [])},
        )()
        self.memory_items = type(
            "M",
            (),
            {
                "typed_recall": method("typed_recall", []),
                "list_current_for_scope": method("list_current_for_scope", []),
            },
        )()
        self.episodes = type(
            "E",
            (),
            {
                "contextual_semantic_search": method("contextual_semantic_search", []),
                "list_recent_contextual": method("list_recent_contextual", []),
                "historical_semantic_search": method("historical_semantic_search", []),
                "list_recent_historical": method("list_recent_historical", []),
                "support_modes": method("support_modes", {}),
            },
        )()

    async def __aenter__(self) -> Uow:
        return self

    async def begin_read_only_snapshot(self) -> None:
        self.calls.append(("begin_read_only_snapshot", {}))

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def no_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(uow: Any, modes: Any) -> dict[UUID, None]:
        return {owner: None for owner in modes}

    monkeypatch.setattr(module, "hydrate_episode_support", none)


def run(
    client: CountingEmbeddingClient, retriever: SpyRetriever, uow: Uow
) -> ContextualRecallService:
    return ContextualRecallService(
        make_settings(),
        embedding_client=client,
        unit_of_work_factory=lambda: uow,  # type: ignore[arg-type,return-value]
        knowledge_retriever=retriever,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_hydration")
@pytest.mark.parametrize("intent", ["resume", "context", "explain", "history"])
async def test_omitted_or_empty_datasets_never_reach_the_knowledge_retriever(intent: str) -> None:
    for datasets in (None, []):
        retriever, uow = SpyRetriever([DatasetSelection(uuid4(), "main")]), Uow()
        overrides: dict[str, Any] = {"intent": intent}
        if datasets is not None:
            overrides["datasets"] = datasets
        bundle = await run(CountingEmbeddingClient(), retriever, uow).recall(request(**overrides))
        assert retriever.resolved == [] and retriever.retrieved == []  # even if 'main' exists
        assert bundle.knowledge_references == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_hydration")
async def test_one_shared_embedding_feeds_memories_episodes_and_knowledge() -> None:
    client, retriever, uow = CountingEmbeddingClient(), SpyRetriever([DS_A]), Uow()
    await run(client, retriever, uow).recall(request(intent="context", datasets=["docs"]))
    assert len(client.calls) == 1
    calls = dict(uow.calls)
    assert retriever.resolved == [["docs"]]
    (knowledge,) = retriever.retrieved
    assert knowledge["datasets"] == [DS_A] and knowledge["query"] == "Onde paramos?"
    assert knowledge["query_embedding"] is calls["typed_recall"]["query_embedding"]
    assert knowledge["query_embedding"] is calls["contextual_semantic_search"]["query_embedding"]
    names = [name for name, _ in uow.calls]
    assert names[0] == "begin_read_only_snapshot"  # datasets validated inside the snapshot


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_hydration")
async def test_embedding_failure_degrades_knowledge_to_lexical_with_one_warning() -> None:
    client, retriever, uow = CountingEmbeddingClient(fail=True), SpyRetriever([DS_A]), Uow()
    bundle = await run(client, retriever, uow).recall(request(intent="explain", datasets=["docs"]))
    assert len(client.calls) == 1  # no retry, no second provider call
    assert retriever.retrieved[0]["query_embedding"] is None
    assert warnings(bundle) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert [s.value for s in bundle.degraded_sources] == ["memories", "episodes", "knowledge"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_hydration")
async def test_history_reads_historical_primitives_and_no_cognitive_memory() -> None:
    for fail in (False, True):
        uow = Uow()
        await run(CountingEmbeddingClient(fail=fail), SpyRetriever(), uow).recall(
            request(intent="history", context_key="feature:a", limits={"historical_checkpoints": 3})
        )
        names = [name for name, _ in uow.calls]
        assert "typed_recall" not in names and "list_current_for_scope" not in names
        assert ("historical_semantic_search" in names) is not fail
        assert ("list_recent_historical" in names) is fail
        history_call = dict(uow.calls)["list_history"]
        assert history_call == {"scope": "project:x", "context_key": "feature:a", "limit": 3}


# --- retriever ------------------------------------------------------------------------------


def chunk(chunk_id: UUID, dataset_id: UUID = DS_A.dataset_id) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        dataset_id=dataset_id,
        source_id=uuid4(),
        source_name="s",
        source_url=None,
        document_id=uuid4(),
        ordinal=0,
        text="conteúdo integral que nunca sai",
        start_char=0,
        end_char=10,
    )


class ChunkUow:
    def __init__(self, vector: list[RetrievedChunk], lexical: list[RetrievedChunk]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class Chunks:
            async def vector_search(self, **kwargs: Any) -> list[RetrievedChunk]:
                outer.calls.append(("vector", kwargs))
                return vector

            async def lexical_search(self, **kwargs: Any) -> list[RetrievedChunk]:
                outer.calls.append(("lexical", kwargs))
                return lexical

        class Datasets:
            def __init__(self) -> None:
                self.rows: dict[str, Any] = {}

            async def get_by_slug(self, slug: str) -> Any:
                return self.rows.get(slug)

        self.chunks = Chunks()
        self.datasets = Datasets()


@pytest.mark.asyncio
async def test_retriever_fuses_locally_dedupes_and_bounds() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    uow = ChunkUow([chunk(a), chunk(b)], [chunk(b), chunk(c)])
    hits = await ContextualKnowledgeRetriever(rrf_k=60).retrieve(
        uow,  # type: ignore[arg-type]
        datasets=[DS_A],
        query="q",
        query_embedding=[0.1, 0.2],
        limit=2,
    )
    assert [h.chunk_id for h in hits] == [b, a]  # b found by both channels ranks first
    assert [h.rank for h in hits] == [1, 2] and hits[0].relevance > hits[1].relevance
    vector_call = dict(uow.calls)["vector"]
    assert vector_call["dataset_ids"] == [DS_A.dataset_id] and vector_call["limit"] == 6


@pytest.mark.asyncio
async def test_retriever_lexical_only_without_embedding_and_nothing_without_input() -> None:
    a = uuid4()
    uow = ChunkUow([chunk(uuid4())], [chunk(a)])
    retriever = ContextualKnowledgeRetriever(rrf_k=60)
    hits = await retriever.retrieve(
        uow,
        datasets=[DS_A],
        query="q",
        query_embedding=None,
        limit=5,  # type: ignore[arg-type]
    )
    assert [h.chunk_id for h in hits] == [a]
    assert [name for name, _ in uow.calls] == ["lexical"]
    empty = ChunkUow([chunk(uuid4())], [chunk(uuid4())])
    assert (
        await retriever.retrieve(empty, datasets=[], query="q", query_embedding=None, limit=5) == []
    )  # type: ignore[arg-type]
    assert (
        await retriever.retrieve(empty, datasets=[DS_A], query="", query_embedding=None, limit=5)
        == []
    )  # type: ignore[arg-type]
    assert empty.calls == []  # never arbitrary / recent chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, DatasetStatus.DELETING, DatasetStatus.DELETED])
async def test_missing_or_inactive_dataset_fails_explicitly(status: DatasetStatus | None) -> None:
    uow = ChunkUow([], [])
    uow.datasets.rows["ok"] = type(
        "D", (), {"id": uuid4(), "slug": "ok", "status": DatasetStatus.ACTIVE}
    )()
    if status is not None:
        uow.datasets.rows["bad"] = type("D", (), {"id": uuid4(), "slug": "bad", "status": status})()
    retriever = ContextualKnowledgeRetriever(rrf_k=60)
    assert [d.slug for d in await retriever.resolve_datasets(uow, ["ok"])] == ["ok"]  # type: ignore[arg-type]
    with pytest.raises(SofiasMemoryError) as excinfo:
        await retriever.resolve_datasets(uow, ["ok", "bad"])  # type: ignore[arg-type]
    assert excinfo.value.code.value == "INVALID_REQUEST" and excinfo.value.status_code == 422
    assert excinfo.value.details == {"field": "datasets", "dataset": "bad"}


def _imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_knowledge_retriever_reuses_only_the_rank_fusion_primitive() -> None:
    imported = _imports(str(retriever_module.__file__))
    from_recall = {name for name in imported if name.startswith("sofias_memory.services.recall")}
    assert from_recall == {"sofias_memory.services.recall.reciprocal_rank_fusion"}
    for path in (retriever_module.__file__, trace_module.__file__):
        for name in _imports(str(path)):
            for forbidden in (
                "sofias_memory.infrastructure.llm",
                "sofias_memory.infrastructure.neo4j",
                "sofias_memory.services.sessions",
                "sofias_memory.services.pipeline_submission",
                "sofias_memory.services.recall.RecallService",
            ):
                assert not name.startswith(forbidden), (path, name)


def test_occurred_at_tie_break_helper_is_deterministic() -> None:
    ids = sorted([uuid4() for _ in range(3)])
    items = [
        module.ContextRecallEpisode(episode=episode(episode_id=i, minutes=5), relevance=None)
        for i in reversed(ids)
    ]
    assert [e.episode.episode_id for e in module.chronological(items)] == ids
    later = episode(minutes=1)
    assert (
        module.chronological([*items, module.ContextRecallEpisode(episode=later, relevance=None)])[
            0
        ].episode.episode_id
        == later.episode_id
    )
    assert timedelta(minutes=1) > timedelta(0)
