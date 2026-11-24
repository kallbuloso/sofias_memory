"""PR #4 corrective proof (SM-CTX-007 explain trace, review findings B and C).

B -- a persisted ``support_mode`` whose Evidence links are missing is an
invariant violation, never a legitimate independent leaf: the explanation is
signalled incomplete (``PARTIAL_CONTEXT``), logged by id, nothing is invented.

C -- the bounded trace is read from the *selected* roots (the same
deterministic selection the composition applies), never from the Episode
candidate window, so discarded candidates never consume traversal capacity.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from sofias_memory.domain import EvidenceSourceType, SupportMode
from sofias_memory.schemas.context_recall import ContextRecallLimits
from sofias_memory.schemas.contextual_evidence import SupportResult
from sofias_memory.services import contextual_evidence_trace as trace_module
from sofias_memory.services import contextual_recall as module
from sofias_memory.services.contextual_evidence_trace import (
    EXPLAIN_TRACE_MAX_EPISODES,
)
from sofias_memory.services.contextual_recall import (
    EpisodeCandidate,
    compose_explain_bundle,
)
from tests.unit.test_contextual_recall_service import checkpoint, episode, request, stmt, sup
from tests.unit.test_contextual_recall_sm_ctx_007 import owners_of, ref_to, snap, warnings

EP = EvidenceSourceType.EPISODE
CH = EvidenceSourceType.CHUNK


class GraphUow:
    """Persisted Episode rows for the trace: ``modes`` is the persisted
    ``support_mode`` column, ``links`` the persisted Evidence links. Read-only
    by construction (it has no write method at all); counts every read."""

    def __init__(
        self,
        modes: dict[UUID, SupportMode | None],
        links: dict[UUID, SupportResult] | None = None,
    ) -> None:
        self.modes = modes
        self.links = links or {}
        self.reads: list[list[UUID]] = []
        outer = self

        class Episodes:
            async def support_modes(self, ids: Sequence[UUID]) -> dict[UUID, Any]:
                outer.reads.append(list(ids))
                return {i: outer.modes.get(i) for i in ids}

        self.episodes = Episodes()


@pytest.fixture()
def graph_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hydration over GraphUow with the production semantics: a declared mode
    without links hydrates to ``None`` -- exactly like an independent leaf."""

    async def hydrate(uow: GraphUow, modes: dict[UUID, Any]) -> dict[UUID, Any]:
        return {owner: (uow.links.get(owner) if mode else None) for owner, mode in modes.items()}

    monkeypatch.setattr(trace_module, "hydrate_episode_support", hydrate)


def graph(*chains: dict[UUID, SupportResult]) -> GraphUow:
    links: dict[UUID, SupportResult] = {}
    for chain in chains:
        links.update(chain)
    return GraphUow({owner: support.mode for owner, support in links.items()}, links)


async def explain(uow: GraphUow, s: Any, **overrides: Any) -> tuple[Any, Any]:
    limits = overrides.pop("limits", ContextRecallLimits())
    req = request(intent="explain", include_evidence=True, **overrides)
    snapshot, bundle = await compose_explain_bundle(
        uow,  # type: ignore[arg-type]
        req,
        limits.effective(),
        s,
        semantic_degraded=False,
    )
    return snapshot, bundle


def evidence_ids(bundle: Any) -> set[UUID]:
    return {item.source_id for item in bundle.evidence}


def _violations(logs: Sequence[Any]) -> list[Any]:
    return [e for e in logs if e.get("event") == "context_recall_invariant_violation"]


# --- B: declared support without persisted links -------------------------------------


def b_chain(a_mode: SupportMode | None) -> tuple[GraphUow, Any, dict[str, UUID]]:
    """S -> B (returned) -> A; A has persisted ``a_mode`` but no links."""

    b, a = uuid4(), uuid4()
    b_support = sup(SupportMode.ALL_REQUIRED, ref_to(EP, a))
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, b)))
    uow = GraphUow({b: SupportMode.ALL_REQUIRED, a: a_mode}, {b: b_support})
    s = snap(
        active_checkpoints=[checkpoint(current_state=cs)],
        episodes=[EpisodeCandidate(episode(episode_id=b, support=b_support), 0.9)],
    )
    return uow, s, {"s": cs.statement_id, "b": b, "a": a}


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_episode_without_declared_support_stays_a_legitimate_leaf() -> None:
    uow, s, ids = b_chain(None)
    with capture_logs() as logs:
        snapshot, bundle = await explain(uow, s)
    assert snapshot.trace.declared_without_links == frozenset()
    assert owners_of(bundle, ids["a"]) == [("episode", ids["b"], "all_required")]
    assert warnings(bundle) == [] and _violations(logs) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
@pytest.mark.parametrize("mode", [SupportMode.ALL_REQUIRED, SupportMode.ANY_SUFFICIENT])
async def test_declared_support_with_zero_links_is_never_a_complete_trace(
    mode: SupportMode,
) -> None:
    uow, s, ids = b_chain(mode)
    with capture_logs() as logs:
        snapshot, bundle = await explain(uow, s)
    assert snapshot.trace.declared_without_links == frozenset({ids["a"]})
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]  # existing warning, nothing new
    assert bundle.degraded_sources == []
    # The persisted edges are kept; nothing is invented below A.
    assert evidence_ids(bundle) == {ids["b"], ids["a"]}
    assert owners_of(bundle, ids["a"]) == [("episode", ids["b"], "all_required")]
    (violation,) = _violations(logs)
    assert violation["reason"] == "declared_support_without_links"
    assert violation["owner_ids"] == [str(ids["a"])]
    assert set(violation) <= {"event", "log_level", "owner_kind", "owner_ids", "reason"}
    assert uow.modes[ids["a"]] is mode  # never repaired


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_inconsistent_intermediate_absent_from_episodes_never_leaks_payload() -> None:
    a, b = uuid4(), uuid4()
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, b)))
    b_support = sup(SupportMode.ALL_REQUIRED, ref_to(EP, a))
    uow = GraphUow({b: SupportMode.ALL_REQUIRED, a: SupportMode.ALL_REQUIRED}, {b: b_support})
    unreturned_a = episode(episode_id=a).model_copy(update={"summary": "RESUMO-INTERMEDIARIO-A"})
    s = snap(active_checkpoints=[checkpoint(current_state=cs)])  # no Episode returned
    _, bundle = await explain(uow, s)
    assert bundle.episodes == []
    assert evidence_ids(bundle) == {a, b}
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]
    assert unreturned_a.summary not in bundle.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_consistent_s_b_a_k_chain_still_complete() -> None:
    b, a, k = uuid4(), uuid4(), uuid4()
    links = {
        b: sup(SupportMode.ALL_REQUIRED, ref_to(EP, a)),
        a: sup(SupportMode.ANY_SUFFICIENT, ref_to(CH, k)),
    }
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, b)))
    s = snap(
        active_checkpoints=[checkpoint(current_state=cs)],
        episodes=[EpisodeCandidate(episode(episode_id=b, support=links[b]), 0.9)],
    )
    _, bundle = await explain(graph(links), s, limits=ContextRecallLimits(episodes=1))
    assert owners_of(bundle, b) == [("context_statement", cs.statement_id, "all_required")]
    assert owners_of(bundle, a) == [("episode", b, "all_required")]
    assert owners_of(bundle, k) == [("episode", a, "any_sufficient")]
    assert warnings(bundle) == []


# --- C: selected trace roots ---------------------------------------------------------


def window_scenario() -> tuple[GraphUow, Any, dict[str, Any]]:
    """limits.episodes=8, 24 candidates: 8 eligible returned ones, then 16
    discarded ones with 17 Episode Evidence refs each (272 parents). The
    returned checkpoint depends on X -> Y -> Chunk K."""

    x, y, k = uuid4(), uuid4(), uuid4()
    links: dict[UUID, SupportResult] = {
        x: sup(SupportMode.ALL_REQUIRED, ref_to(EP, y)),
        y: sup(SupportMode.ALL_REQUIRED, ref_to(CH, k)),
    }
    returned = [EpisodeCandidate(episode(support=None), 0.9) for _ in range(8)]
    discarded: list[EpisodeCandidate] = []
    parents: set[UUID] = set()
    for _ in range(16):
        ids = [uuid4() for _ in range(17)]
        parents.update(ids)
        refs = [ref_to(EP, i) for i in ids]
        discarded.append(
            EpisodeCandidate(episode(support=sup(SupportMode.ANY_SUFFICIENT, *refs)), 0.5)
        )
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, x)))
    uow = GraphUow(
        {**{p: None for p in parents}, x: SupportMode.ALL_REQUIRED, y: SupportMode.ALL_REQUIRED},
        links,
    )
    s = snap(active_checkpoints=[checkpoint(current_state=cs)], episodes=[*returned, *discarded])
    return uow, s, {"x": x, "y": y, "k": k, "parents": parents, "returned": returned}


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_discarded_candidates_never_consume_trace_capacity() -> None:
    uow, s, ids = window_scenario()
    assert len(ids["parents"]) + 1 > EXPLAIN_TRACE_MAX_EPISODES  # the old failure mode
    snapshot, bundle = await explain(uow, s, limits=ContextRecallLimits(episodes=8))
    assert [e.episode.episode_id for e in bundle.episodes] == [
        c.episode.episode_id for c in ids["returned"]
    ]
    read = {i for batch in uow.reads for i in batch}
    assert read == {ids["x"], ids["y"]}  # no discarded candidate ancestry is ever read
    assert not (read & ids["parents"])
    assert snapshot.trace.cut == frozenset()
    assert ids["k"] in evidence_ids(bundle)
    assert owners_of(bundle, ids["k"]) == [("episode", ids["y"], "all_required")]
    assert warnings(bundle) == []  # no spurious PARTIAL_CONTEXT


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_selected_trace_is_deterministic_under_candidate_reordering() -> None:
    uow, s, _ = window_scenario()
    returned, discarded = s.episodes[:8], s.episodes[8:]
    dumps = set()
    for seed in (1, 2, 3):
        rng = random.Random(seed)
        shuffled = []
        for candidate in rng.sample(discarded, len(discarded)):
            support = candidate.episode.support
            refs = rng.sample(support.evidence, len(support.evidence))
            moved = candidate.episode.model_copy(
                update={"support": support.model_copy(update={"evidence": refs})}
            )
            shuffled.append(EpisodeCandidate(moved, candidate.relevance))
        reordered = snap(active_checkpoints=s.active_checkpoints, episodes=[*returned, *shuffled])
        _, bundle = await explain(uow, reordered, limits=ContextRecallLimits(episodes=8))
        dumps.add(bundle.model_dump_json(include={"evidence", "warnings", "degraded_sources"}))
    assert len(dumps) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_no_episode_selected_traces_the_checkpoint_only() -> None:
    uow, s, ids = window_scenario()
    s = snap(active_checkpoints=s.active_checkpoints, episodes=[])
    _, bundle = await explain(uow, s)
    assert bundle.episodes == [] and ids["k"] in evidence_ids(bundle)
    assert warnings(bundle) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_one_selected_episode_and_multiple_selected_roots() -> None:
    def chain() -> tuple[UUID, UUID, dict[UUID, SupportResult], EpisodeCandidate]:
        a, k = uuid4(), uuid4()
        links = {a: sup(SupportMode.ALL_REQUIRED, ref_to(CH, k))}
        root = EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, ref_to(EP, a))), 0.9)
        return a, k, links, root

    a1, k1, l1, r1 = chain()
    a2, k2, l2, r2 = chain()
    heavy_parents = [uuid4() for _ in range(40)]
    heavy = EpisodeCandidate(
        episode(support=sup(SupportMode.ANY_SUFFICIENT, *(ref_to(EP, p) for p in heavy_parents))),
        0.1,
    )
    uow = graph(l1, l2)
    uow.modes.update(dict.fromkeys(heavy_parents))
    _, one = await explain(
        uow, snap(episodes=[r1, r2, heavy]), limits=ContextRecallLimits(episodes=1)
    )
    assert evidence_ids(one) == {a1, k1} and warnings(one) == ["NO_CURRENT_CHECKPOINT"]
    assert {i for batch in uow.reads for i in batch} == {a1}
    uow.reads.clear()
    _, both = await explain(
        uow, snap(episodes=[r1, r2, heavy]), limits=ContextRecallLimits(episodes=2)
    )
    assert evidence_ids(both) == {a1, k1, a2, k2}
    assert warnings(both) == ["NO_CURRENT_CHECKPOINT"]
    assert {i for batch in uow.reads for i in batch} == {a1, a2}


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_evidence_limit_smaller_than_the_selected_trace_is_partial_and_coherent() -> None:
    uow, s, ids = window_scenario()
    _, bundle = await explain(uow, s, limits=ContextRecallLimits(episodes=8, evidence=2))
    assert evidence_ids(bundle) == {ids["x"], ids["y"]}  # coherent prefix, K dropped
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_corrupt_cycle_only_among_discarded_candidates_does_not_affect_the_trace() -> None:
    uow, s, ids = window_scenario()
    p, q = uuid4(), uuid4()
    uow.links[p] = sup(SupportMode.ALL_REQUIRED, ref_to(EP, q))  # controlled corruption
    uow.links[q] = sup(SupportMode.ALL_REQUIRED, ref_to(EP, p))
    uow.modes.update({p: SupportMode.ALL_REQUIRED, q: SupportMode.ALL_REQUIRED})
    cyclic = EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, ref_to(EP, p))), 0.4)
    s = snap(active_checkpoints=s.active_checkpoints, episodes=[*s.episodes, cyclic])
    snapshot, bundle = await explain(uow, s, limits=ContextRecallLimits(episodes=8))
    assert snapshot.trace.cycle is False and warnings(bundle) == []
    assert not ({p, q} & {i for batch in uow.reads for i in batch})


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_genuinely_truncated_selected_trace_still_signals_partial() -> None:
    chain = [uuid4() for _ in range(11)]
    links = {chain[i]: sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[i + 1])) for i in range(10)}
    cs = stmt("estado", sup(SupportMode.ALL_REQUIRED, ref_to(EP, chain[0])))
    snapshot, bundle = await explain(
        graph(links), snap(active_checkpoints=[checkpoint(current_state=cs)])
    )
    assert snapshot.trace.cut  # depth bound of the selected trace itself
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("graph_hydration")
async def test_budget_dropped_roots_are_re_read_so_kept_roots_get_the_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    x, y, k = uuid4(), uuid4(), uuid4()
    links = {
        x: sup(SupportMode.ALL_REQUIRED, ref_to(EP, y)),
        y: sup(SupportMode.ALL_REQUIRED, ref_to(CH, k)),
    }
    kept = EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, ref_to(EP, x))), 0.9)
    heavy: list[EpisodeCandidate] = []
    modes: dict[UUID, Any] = {x: SupportMode.ALL_REQUIRED, y: SupportMode.ALL_REQUIRED}
    for _ in range(7):
        parents = [uuid4() for _ in range(50)]
        modes.update(dict.fromkeys(parents))
        heavy.append(
            EpisodeCandidate(
                episode(
                    support=sup(SupportMode.ANY_SUFFICIENT, *(ref_to(EP, p) for p in parents)),
                ).model_copy(update={"summary": "x" * 4000}),
                0.5,
            )
        )
    uow = GraphUow(modes, links)
    s = snap(episodes=[kept, *heavy])
    limits = ContextRecallLimits(episodes=8)
    _, alone = await explain(GraphUow(modes, links), snap(episodes=[kept]), limits=limits)
    budget = module.serialized_bundle_bytes(alone) + 200
    original = module.compose_bundle

    def small(*args: Any, **kwargs: Any) -> Any:
        return original(*args, max_bundle_bytes=budget, **kwargs)

    monkeypatch.setattr(module, "compose_bundle", small)
    snapshot, bundle = await explain(uow, s, limits=limits)
    assert [e.episode.episode_id for e in bundle.episodes] == [kept.episode.episode_id]
    assert warnings(bundle) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]  # omission signalled
    assert snapshot.trace.cut == frozenset()  # re-read from the kept roots only
    assert k in evidence_ids(bundle)
    assert evidence_ids(bundle) == evidence_ids(alone)
