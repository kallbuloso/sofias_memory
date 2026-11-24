"""Bounded, read-only Evidence traversal for Contextual Recall ``explain``
(SM-CTX-007, handoff 15 SS 10-24/83-88/107/143-145).

Causality comes only from explicit Evidence links (``EvidenceRef``) -- never
from checkpoint lineage (``superseded_by``/revision order), timestamps,
Sessions, similarity or provenance. Starting from the support of the selected
contextual resources, the traversal follows Episode -> Evidence edges
breadth-first, PostgreSQL-only, inside the caller's read-only snapshot:

- only through Episodes whose Evidence availability is AVAILABLE (an
  INVALIDATED Episode is UNAVAILABLE and never acts as support; a FORGOTTEN
  one is an identity-only leaf); MemoryItem, Session and Knowledge Evidence are
  leaves (Cognitive Memory has no support graph in Contract v1);
- bounded in depth (``EXPLAIN_TRACE_MAX_DEPTH`` hops) and breadth (at most
  ``EXPLAIN_TRACE_MAX_EPISODES`` expanded Episodes); anything not expanded is
  recorded in ``cut`` so a truncated trace is never presented as complete;
- set semantics (each Episode expanded once) terminate even on a corrupt
  cycle, which is detected and reported (CTX-004 forbids cycles);
- the persisted ``support_mode`` is the authority on whether an Episode
  declares a dependency: a declared mode whose links are missing (an invariant
  violation, never a legitimate independent leaf) is recorded in
  ``declared_without_links`` so the explanation is never presented as complete.

The internal model preserves every edge (owner type/id, support mode, source
type/id, availability). Nothing is mutated or repaired.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from sofias_memory.domain import EvidenceAvailability, EvidenceSourceType, SupportMode
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.contextual_evidence import SupportResult
from sofias_memory.services.contextual_evidence import hydrate_episode_support

EXPLAIN_TRACE_MAX_DEPTH = 8
"""Episode -> Episode hops expanded beyond the selected roots (internal hard
cap; the Contract only bounds the aggregated ``limits.evidence``)."""

EXPLAIN_TRACE_MAX_EPISODES = 256
"""Total Episodes whose own support is expanded for one request (breadth/fan-out
hard cap)."""


@dataclass(frozen=True, slots=True)
class TraceEdge:
    """One explicit dependency: ``owner`` is supported by ``source``."""

    owner_type: str
    owner_id: UUID
    support_mode: SupportMode
    source_type: EvidenceSourceType
    source_id: UUID
    availability: EvidenceAvailability


@dataclass(frozen=True, slots=True)
class EvidenceTrace:
    """The bounded traversal beyond the selected roots.

    ``supports`` holds the hydrated support (``None`` = no declared
    dependency) of every expanded Episode; ``cut`` the Episodes reached but not
    expanded because of a bound; ``cycle`` whether the traced Episode graph is
    cyclic (an invariant violation); ``declared_without_links`` the expanded
    Episodes whose persisted ``support_mode`` is set but which have no persisted
    Evidence link (their ``supports`` entry is ``None``, exactly as for an
    independent leaf, so this set is what tells them apart)."""

    supports: Mapping[UUID, SupportResult | None]
    cut: frozenset[UUID]
    cycle: bool
    declared_without_links: frozenset[UUID] = frozenset()

    def edges(self) -> list[TraceEdge]:
        """Every traced Episode edge in deterministic ``(owner_id, source_type,
        source_id)`` order."""

        out: list[TraceEdge] = []
        for owner_id in sorted(self.supports, key=str):
            support = self.supports[owner_id]
            if support is None:
                continue
            out.extend(
                TraceEdge(
                    owner_type="episode",
                    owner_id=owner_id,
                    support_mode=support.mode,
                    source_type=evidence.source_type,
                    source_id=evidence.source_id,
                    availability=evidence.availability,
                )
                for evidence in support.evidence
            )
        return sorted(out, key=lambda e: (str(e.owner_id), e.source_type.value, str(e.source_id)))


EMPTY_TRACE = EvidenceTrace(supports={}, cut=frozenset(), cycle=False)


def available_episode_targets(support: SupportResult | None) -> list[UUID]:
    """Episodes that this support relies on and that may support in turn."""

    if support is None:
        return []
    return [
        evidence.source_id
        for evidence in support.evidence
        if evidence.source_type is EvidenceSourceType.EPISODE
        and evidence.availability is EvidenceAvailability.AVAILABLE
    ]


def has_cycle(supports: Mapping[UUID, SupportResult | None]) -> bool:
    """Iterative DFS over the traced Episode -> Episode edges (any availability)."""

    graph: dict[UUID, list[UUID]] = {
        owner: [
            e.source_id
            for e in (support.evidence if support else ())
            if e.source_type is EvidenceSourceType.EPISODE
        ]
        for owner, support in supports.items()
    }
    state: dict[UUID, int] = {}  # 1 = on stack, 2 = done
    for start in sorted(graph, key=str):
        if state.get(start):
            continue
        stack: list[tuple[UUID, int]] = [(start, 0)]
        state[start] = 1
        while stack:
            node, index = stack[-1]
            children = graph.get(node, [])
            if index < len(children):
                stack[-1] = (node, index + 1)
                child = children[index]
                mark = state.get(child)
                if mark == 1:
                    return True
                if mark is None and child in graph:
                    state[child] = 1
                    stack.append((child, 0))
            else:
                state[node] = 2
                stack.pop()
    return False


async def read_evidence_trace(
    uow: PostgresUnitOfWork,
    root_supports: Iterable[SupportResult | None],
    *,
    max_depth: int = EXPLAIN_TRACE_MAX_DEPTH,
    max_episodes: int = EXPLAIN_TRACE_MAX_EPISODES,
) -> EvidenceTrace:
    """Breadth-first, level by level (one batched read per level), in
    deterministic id order. Read-only; current availability only."""

    supports: dict[UUID, SupportResult | None] = {}
    cut: set[UUID] = set()
    declared_without_links: set[UUID] = set()
    frontier = sorted(
        {target for support in root_supports for target in available_episode_targets(support)},
        key=str,
    )
    depth = 0
    while frontier:
        if depth >= max_depth:
            cut.update(frontier)
            break
        remaining = max(0, max_episodes - len(supports))
        expand: Sequence[UUID] = frontier[:remaining]
        cut.update(frontier[remaining:])
        if not expand:
            break
        modes = await uow.episodes.support_modes(expand)
        hydrated = await hydrate_episode_support(
            uow, {episode_id: modes.get(episode_id) for episode_id in expand}
        )
        supports.update(hydrated)
        declared_without_links.update(
            episode_id
            for episode_id in expand
            if modes.get(episode_id) is not None and hydrated.get(episode_id) is None
        )
        discovered = {
            target
            for episode_id in expand
            for target in available_episode_targets(hydrated.get(episode_id))
            if target not in supports and target not in cut
        }
        frontier = sorted(discovered, key=str)
        depth += 1
    return EvidenceTrace(
        supports=supports,
        cut=frozenset(cut),
        cycle=has_cycle(supports),
        declared_without_links=frozenset(declared_without_links),
    )


__all__ = [
    "EMPTY_TRACE",
    "EXPLAIN_TRACE_MAX_DEPTH",
    "EXPLAIN_TRACE_MAX_EPISODES",
    "EvidenceTrace",
    "TraceEdge",
    "available_episode_targets",
    "has_cycle",
    "read_evidence_trace",
]
