"""Provenance-aware Forget propagation (SM-CTX-005, handoff 13).

``ContextualEvidenceInvalidator`` is an internal service -- never a public API,
never a worker/queue/trigger (SS 16-18/144). It runs *inside* the authoritative
PostgreSQL transaction of the Forget that caused the Evidence loss, after the
caller has already made its target non-AVAILABLE, so

- direct Cognitive Memory / Episode Forget: HTTP success => target Forget and
  propagation committed together; any propagation failure rolls both back;
- Knowledge Forget / Dataset Delete: the ``authoritative_mutation`` step commits
  Knowledge deactivation and propagation together, before any Neo4j/storage
  work, so ``PipelineRun SUCCEEDED`` implies propagation is durable.

Only explicit Evidence creates a dependency (``Provenance != Evidence``); every
owner reached is re-evaluated over its *complete* support set with the same
deterministic ``evaluate_support`` used by reads (no LLM, no embedding, no
Neo4j, no new ``graph_outbox`` event, no PipelineRun, no per-dependent ledger
entry). Unsupported Episodes become FORGOTTEN tombstones (destructive scrub,
SS 46-49); unsupported ContextStatements are deleted as whole units (SS 53-55);
losing ``current_state`` invalidates the checkpoint without any historical
fallback and preserving supersession lineage (SS 56-60).

Lock order (Critical Gate 2), always after the caller's own target rows and
idempotency claim:

1. Phase 1 -- dependent Episodes, ``FOR UPDATE``, popped from a priority queue
   in ascending ``(rank, id)`` (:func:``episode_ranks``). A dependent always ranks
   strictly above the Episode that exposed it, so the sequence of acquired
   locks is monotonic -- the same order Evidence-aware writers use for their
   ``FOR SHARE`` locks. Each owner is locked *before* its support is read and
   before its own dependents are listed, so (a) a concurrent Create that
   share-locked it first is committed and visible, or waits and then observes
   the tombstone (no Evidence Create escapes propagation, Critical Gate 1),
   and (b) two Forgets converging on one owner evaluate it serially, the
   second seeing the first's committed loss (no write skew under
   ``any_sufficient``).
2. Phase 2 -- checkpoints, ``FOR UPDATE`` in ascending id, after every Episode
   decision is flushed; statements are evaluated under their checkpoint lock.

Outgoing links of scrubbed Episodes are removed only at the phase boundary so
the ranks seen during traversal equal the committed graph seen by concurrent
writers. Traversal is iterative; a cycle, a support mode without links, links
without a mode or a dangling owner raise :class:``EvidenceGraphCorruptionError``
and the whole Forget fails closed (SS 68-69). PostgreSQL errors propagate
unchanged (never persisted as "unavailable", SS 70).
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from sofias_memory.domain import (
    ContextCheckpointLifecycle,
    ContextStatementType,
    EpisodeLifecycle,
    EvidenceGraphCorruptionError,
    EvidenceKey,
    EvidenceSourceType,
    SupportMode,
    episode_ranks,
    evaluate_support,
)
from sofias_memory.infrastructure.postgres.models import Episode, EpisodeProvenance
from sofias_memory.infrastructure.postgres.repositories.contextual_evidence import (
    LinkedEvidence,
)
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.contextual_evidence import EvidenceResolver

_logger = get_logger(__name__)

PROPAGATED_INVALIDATION_REASON = "required evidence is no longer available"
"""Fixed structural reason (handoff 13 SS 62): the schema requires a non-blank
reason for INVALIDATED; it never carries Evidence content and is never exposed."""


@dataclass(slots=True)
class PropagationCounts:
    """Safe observability counters (handoff 13 SS 139) -- never semantic text."""

    evidence_sources_processed: int = 0
    episodes_forgotten: int = 0
    statements_removed: int = 0
    checkpoints_invalidated: int = 0

    def as_log_fields(self) -> dict[str, int]:
        return asdict(self)


def scrub_episode(episode: Episode, provenance: EpisodeProvenance, *, now: datetime) -> None:
    """The one destructive Episode tombstone scrub (Contract v1 SS 21/67, handoff
    13 SS 48): direct Forget and propagation produce the same shape. Outgoing
    Evidence links are removed by the caller (direct Forget: by the invalidator
    at its phase boundary); the inbound canonical identity stays (SS 97)."""

    episode.scope = None
    episode.context_key = None
    episode.kind = None
    episode.summary = None
    episode.situation = None
    episode.reason = None
    episode.outcome = None
    episode.occurred_at = None
    episode.embedding = None
    episode.confidence = None
    episode.invalidation_reason = None
    episode.replacement_episode_id = None
    episode.support_mode = None
    episode.lifecycle = EpisodeLifecycle.FORGOTTEN
    episode.forgotten_at = now
    provenance.conversation_ref = None
    provenance.turn_ref = None


def _integrity(message: str) -> EvidenceGraphCorruptionError:
    return EvidenceGraphCorruptionError(message)


class ContextualEvidenceInvalidator:
    """See module docstring. One instance per Forget transaction."""

    def __init__(self, uow: PostgresUnitOfWork) -> None:
        self._uow = uow
        self._repo = uow.contextual_evidence
        self._resolver = EvidenceResolver(uow)

    async def propagate(
        self,
        seeds: Iterable[EvidenceKey],
        *,
        scrubbed_episode_ids: Sequence[UUID] = (),
    ) -> PropagationCounts:
        """Converge every explicit dependent of ``seeds`` (Evidence the caller
        has just made, or already found, non-AVAILABLE). Idempotent: a second
        run over the same loss performs no further semantic mutation (SS 76).

        ``scrubbed_episode_ids``: Episodes the caller tombstoned in this
        transaction whose outgoing links must be released at the phase
        boundary (direct Episode Forget)."""

        counts = PropagationCounts()
        await self._uow.flush()  # the caller's target mutation must be visible
        now = utc_now()

        lost: list[EvidenceKey] = sorted(set(seeds))
        lost_set: set[EvidenceKey] = set(lost)
        released: set[UUID] = set(scrubbed_episode_ids)
        heap: list[tuple[int, UUID]] = []
        queued: set[UUID] = set()
        floor: list[tuple[int, UUID] | None] = [None]

        async def enqueue_dependents(keys: Sequence[EvidenceKey]) -> None:
            owners = [
                owner
                for owner in await self._repo.episode_owners(keys)
                if owner not in released and owner not in queued
            ]
            if not owners:
                return
            parents = await self._repo.episode_parent_edges(owners)
            ranks = episode_ranks(owners, parents)  # raises on a corrupt cycle
            for owner in owners:
                entry = (ranks[owner], owner)
                current = floor[0]
                if current is not None and entry <= current:
                    # A dependent can never rank at or below the Episode that
                    # exposed it in an acyclic graph.
                    raise _integrity("Episode Evidence dependency order is inconsistent.")
                heapq.heappush(heap, entry)
                queued.add(owner)

        def mark_lost(episode_id: UUID) -> EvidenceKey:
            key = EvidenceKey(EvidenceSourceType.EPISODE, episode_id)
            if key not in lost_set:
                lost_set.add(key)
                lost.append(key)
            released.add(episode_id)
            return key

        # --- Phase 1: Episodes, ascending (rank, id) -----------------------------
        await enqueue_dependents(lost)
        while heap:
            entry = heapq.heappop(heap)
            queued.discard(entry[1])
            floor[0] = entry
            episode_id = entry[1]
            episode, provenance = await self._repo.lock_episode_for_propagation(episode_id)
            if episode is None or provenance is None:
                raise _integrity("Evidence owner Episode is missing.")
            if episode.lifecycle is EpisodeLifecycle.FORGOTTEN:
                # Residual links on a tombstone (pre-SM-CTX-005 data): no domain
                # change, but it still converges as a transitive seed (SS 50).
                await enqueue_dependents([mark_lost(episode_id)])
                continue
            links = await self._repo.list_episode_links([episode_id])
            if await self._is_sufficient(episode.support_mode, links):
                continue
            scrub_episode(episode, provenance, now=now)
            await self._uow.flush()  # SS 93: later resolutions must observe it
            counts.episodes_forgotten += 1
            await enqueue_dependents([mark_lost(episode_id)])

        for episode_id in sorted(released):
            await self._repo.delete_episode_links(episode_id)
        await self._uow.flush()

        # --- Phase 2: ContextStatements under their checkpoint lock ----------------
        by_checkpoint: dict[UUID, list[UUID]] = defaultdict(list)
        for checkpoint_id, statement_id in await self._repo.statement_owners(lost):
            by_checkpoint[checkpoint_id].append(statement_id)
        for checkpoint_id in sorted(by_checkpoint):
            checkpoint = await self._repo.lock_checkpoint_for_propagation(checkpoint_id)
            if checkpoint is None:
                raise _integrity("Evidence owner checkpoint is missing.")
            statements = await self._repo.statements_by_ids(by_checkpoint[checkpoint_id])
            links = await self._repo.list_statement_links([s.id for s in statements])
            grouped: dict[UUID, list[LinkedEvidence]] = defaultdict(list)
            for link in links:
                grouped[link.owner_id].append(link)
            resolutions = await self._resolver.resolve({link.key for link in links}, lock=False)
            removed: list[UUID] = []
            lost_current_state = False
            for statement in statements:
                owner_links = grouped.get(statement.id, [])
                mode = self._checked_mode(statement.support_mode, owner_links)
                if evaluate_support(
                    mode, [resolutions[link.key].availability for link in owner_links]
                ):
                    continue
                removed.append(statement.id)
                if statement.statement_type is ContextStatementType.CURRENT_STATE:
                    lost_current_state = True
            if not removed:
                continue
            await self._repo.delete_statements(removed)
            counts.statements_removed += len(removed)
            if lost_current_state and checkpoint.lifecycle in (
                ContextCheckpointLifecycle.ACTIVE,
                ContextCheckpointLifecycle.SUPERSEDED,
            ):
                # superseded_at/superseded_by are never touched (SS 59); no
                # earlier revision is ever reactivated (SS 58).
                checkpoint.lifecycle = ContextCheckpointLifecycle.INVALIDATED
                checkpoint.invalidated_at = now
                checkpoint.invalidation_reason = PROPAGATED_INVALIDATION_REASON
                counts.checkpoints_invalidated += 1
            await self._uow.flush()

        counts.evidence_sources_processed = len(lost)
        _logger.info("contextual_evidence_propagated", **counts.as_log_fields())
        return counts

    @staticmethod
    def _checked_mode(mode: SupportMode | None, links: Sequence[LinkedEvidence]) -> SupportMode:
        if mode is None or not links:
            raise _integrity("Evidence owner support declaration is inconsistent.")
        return mode

    async def _is_sufficient(
        self, mode: SupportMode | None, links: Sequence[LinkedEvidence]
    ) -> bool:
        checked = self._checked_mode(mode, links)
        resolutions = await self._resolver.resolve({link.key for link in links}, lock=False)
        return evaluate_support(checked, [resolutions[link.key].availability for link in links])


async def propagate_knowledge_evidence_loss(
    uow: PostgresUnitOfWork, source_ids: Sequence[UUID]
) -> PropagationCounts:
    """Knowledge seed discovery (handoff 13 SS 30-34/81): every canonical
    ``source``/``document``/``chunk`` ref structurally belonging to the affected
    Sources, any generation. Shared by Source/Dataset/Everything Forget and
    Administrative Dataset Delete (one algorithm, SS 25/34)."""

    seeds = await uow.contextual_evidence.knowledge_seed_keys(source_ids)
    if not seeds:
        return PropagationCounts()
    return await ContextualEvidenceInvalidator(uow).propagate(seeds)
