"""Contextual Recall (SM-CTX-006 core + SM-CTX-007 explain/history/Knowledge,
Contract v1 Contextual Project Memory SS 37-53, Architecture SS 10/33-38/42/61/
67/69, handoffs 14 and 15).

``ContextualRecallService`` is a synchronous, read-only orchestrator over
first-party sources chosen deterministically by intent (no LLM router):

- ``resume``/``context``: current checkpoint, current project Cognitive
  Memory, ACTIVE Episodes;
- ``explain``: the same, plus a bounded explicit-Evidence trace
  (``services.contextual_evidence_trace``) -- causality only from Evidence,
  never from lineage, chronology or similarity;
- ``history``: current checkpoint as orientation, SUPERSEDED/INVALIDATED
  checkpoint revisions of the stream, ACTIVE/INVALIDATED Episodes in
  chronological order, no current memories;
- every intent: Knowledge references only for explicitly selected ACTIVE
  datasets (``services.contextual_knowledge_retriever``), never ``main`` by
  default, never inferred from the project scope.

It owns no corpus, never generates an answer and never mutates anything: no
Query/PipelineRun/PipelineStep/Session/SessionEntry/MemoryItem/Episode/
ContextCheckpoint/Evidence/graph_outbox row, no LLM, no RecallService, no
Neo4j. It is deliberately separate from ``services.recall`` (Knowledge
Recall: Query audit, graph, RAG, Session admission).

Flow (handoff 14 SS 56, handoff 15 SS 57/105): at most one query embedding,
before any PostgreSQL session opens -> one short read-only snapshot (never
committed) validating datasets and reading every source -> pure,
deterministic composition. A failed embedding is never retried: the
bundle degrades to the deterministic bounded fallback with ``PARTIAL_CONTEXT``
and the affected ``degraded_sources`` (never a silent fallback). PostgreSQL
failures propagate -- the database is the authority of the whole bundle.

Eligibility (handoff 14 SS 63, Contract SS 51.1): persistence lifecycle is not
recall eligibility. An ACTIVE Episode or a ContextStatement whose explicit
support is currently insufficient is excluded from the usable context
(``EVIDENCE_UNAVAILABLE`` + ``PARTIAL_CONTEXT``); without an eligible
``current_state`` there is no current checkpoint (``continuity_available=false``,
``NO_CURRENT_CHECKPOINT``) and never a historical fallback. ``history`` may show
remaining historical content whose ordinary support is insufficient, with its
lifecycle and Evidence availability (``EVIDENCE_UNAVAILABLE``), but content that
a successful Forget should have scrubbed is never exposed in any intent. Rows
are never mutated or "repaired" during a read. Collections are ranked per
source (no cross-source score competition) and each is bounded independently.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from http import HTTPStatus
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CognitiveMemoryType,
    EvidenceAvailability,
    EvidenceKey,
    EvidenceSourceType,
    SupportMode,
    evaluate_support,
)
from sofias_memory.infrastructure.postgres.repositories.contextual_evidence import (
    KnowledgeLocation,
)
from sofias_memory.infrastructure.postgres.repositories.episodes import (
    EpisodeRow,
    EpisodeSemanticHit,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointResult,
    ContextStatementResult,
)
from sofias_memory.schemas.context_recall import (
    ContextRecallCheckpoint,
    ContextRecallEpisode,
    ContextRecallEvidence,
    ContextRecallEvidenceOwner,
    ContextRecallHistoricalCheckpoint,
    ContextRecallIntent,
    ContextRecallKnowledgeReference,
    ContextRecallLimits,
    ContextRecallMemory,
    ContextRecallRequest,
    ContextRecallSource,
    ContextRecallWarning,
    ProjectContextBundle,
)
from sofias_memory.schemas.contextual_evidence import EvidenceResult, SupportResult
from sofias_memory.schemas.episodes import EpisodeResult
from sofias_memory.schemas.memories import MemoryItemResult
from sofias_memory.services.cognitive_memory import EmbeddingClient, memory_item_result
from sofias_memory.services.context_checkpoints import hydrate_checkpoints_with_support_modes
from sofias_memory.services.contextual_evidence import hydrate_episode_support, is_supported
from sofias_memory.services.contextual_evidence_trace import (
    EvidenceTrace,
    read_evidence_trace,
)
from sofias_memory.services.contextual_knowledge_retriever import (
    ContextualKnowledgeRetriever,
    DatasetSelection,
    KnowledgeHit,
)
from sofias_memory.services.episodes import episode_result

type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]

SUPPORTED_INTENTS = frozenset(ContextRecallIntent)
"""All four Contract v1 intents (explain/history since SM-CTX-007)."""

KNOWLEDGE_EVIDENCE_TYPES = frozenset(
    {EvidenceSourceType.SOURCE, EvidenceSourceType.DOCUMENT, EvidenceSourceType.CHUNK}
)

CONTEXT_RECALL_MEMORY_TYPES: tuple[CognitiveMemoryType, ...] = (
    CognitiveMemoryType.PROFILE,
    CognitiveMemoryType.SEMANTIC,
)

EPISODE_CANDIDATE_WINDOW_FACTOR = 3
EPISODE_CANDIDATE_WINDOW_MAX = 150
"""Episodes are read in a bounded candidate window (``limit * 3``, at most 150)
so that excluding currently unsupported Episodes rarely leaves the requested
limit unfilled. Never unbounded."""

CONTEXT_RECALL_MAX_BUNDLE_BYTES = 256 * 1024
"""Internal operational bound (handoff 14 SS 86) of the serialized
``ProjectContextBundle`` (UTF-8 JSON, the ``data`` of the envelope). Not a
public Contract field: resources that do not fit are omitted whole -- never
truncated -- in deterministic priority order (checkpoint, memories, Episodes)
and the omission is signalled with ``PARTIAL_CONTEXT``."""

CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS = 64
"""Internal bound on the statements of a returned checkpoint (all sections).
A larger eligible checkpoint is omitted whole -- never partially rebuilt."""

_logger = get_logger(__name__)


# --- source snapshot (read inside one short unit of work) ---------------------------


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory: MemoryItemResult
    relevance: float | None


@dataclass(frozen=True, slots=True)
class EpisodeCandidate:
    episode: EpisodeResult
    relevance: float | None
    support_declared: bool = False
    """Persisted ``support_mode`` is set. With ``support=None`` (no links read)
    it is an invariant violation, never an independent assertion."""


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Everything read from PostgreSQL for one recall, already converted to
    public shapes with *current* Evidence availability. Candidates are in
    their source-specific deterministic order."""

    active_checkpoints: list[ContextCheckpointResult]
    memories: list[MemoryCandidate]
    episodes: list[EpisodeCandidate]
    declared_statement_ids: frozenset[UUID] = frozenset()
    """Statements (current and historical checkpoints) whose persisted
    ``support_mode`` is set."""
    historical_checkpoints: list[ContextCheckpointResult] = field(default_factory=list)
    """intent history: SUPERSEDED/INVALIDATED revisions, revision DESC, id ASC."""
    datasets: list[DatasetSelection] = field(default_factory=list)
    """Explicitly selected datasets, validated ACTIVE in this snapshot."""
    knowledge_hits: list[KnowledgeHit] = field(default_factory=list)
    """Knowledge retrieval hits of ``datasets`` in local rank order."""
    knowledge_locations: dict[EvidenceKey, KnowledgeLocation] = field(default_factory=dict)
    """Structural identity of AVAILABLE Source/Document/Chunk Evidence of the
    candidates, inside ``datasets`` only (Evidence-backed references)."""
    trace: EvidenceTrace | None = None
    """intent explain: the bounded Evidence traversal beyond the candidates."""


# --- pure composition -----------------------------------------------------------------


@dataclass(slots=True)
class _Signals:
    warnings: set[ContextRecallWarning] = field(default_factory=set)
    degraded: set[ContextRecallSource] = field(default_factory=set)
    quiet: bool = False
    """Selection only (explain trace roots): the authoritative composition that
    follows logs every invariant violation exactly once."""

    def evidence_unavailable(self) -> None:
        self.warnings.update(
            {ContextRecallWarning.EVIDENCE_UNAVAILABLE, ContextRecallWarning.PARTIAL_CONTEXT}
        )


def violates_forget_invariant(support: SupportResult) -> bool:
    """Whether CTX-005 propagation must already have scrubbed this owner.

    Forget propagation converges every explicit owner of a FORGOTTEN Evidence in
    the Forget's own transaction, so an owner that is insufficient counting
    *only* FORGOTTEN losses (``all_required`` with any FORGOTTEN ref,
    ``any_sufficient`` with every ref FORGOTTEN) cannot legitimately exist after
    a successful Forget. Ordinary invalidation (UNAVAILABLE) never triggers
    propagation, so it is not counted here."""

    forget_only = [
        EvidenceAvailability.FORGOTTEN
        if evidence.availability is EvidenceAvailability.FORGOTTEN
        else EvidenceAvailability.AVAILABLE
        for evidence in support.evidence
    ]
    return not evaluate_support(support.mode, forget_only)


def _usable(
    owner_kind: str,
    owner_id: UUID,
    support: SupportResult | None,
    *,
    declared: bool = False,
    log: bool = True,
) -> bool:
    """``support=None`` with no declared dependency (``support_mode=None``) is
    an independent assertion and usable; insufficient support is excluded. A
    declared ``support_mode`` whose links are missing, and a post-Forget
    invariant violation, are excluded as well and logged (ids only, never
    content) -- recall never repairs them."""

    if support is None and declared:
        if log:
            _logger.error(
                "context_recall_invariant_violation",
                owner_kind=owner_kind,
                owner_id=str(owner_id),
                reason="declared_support_without_links",
            )
        return False
    if is_supported(support) is not False:
        return True
    if log and support is not None and violates_forget_invariant(support):
        _logger.error(
            "context_recall_invariant_violation",
            owner_kind=owner_kind,
            owner_id=str(owner_id),
            reason="forgotten_evidence_not_propagated",
        )
    return False


def select_current_checkpoint(
    active: Sequence[ContextCheckpointResult],
    signals: _Signals,
    declared_statement_ids: frozenset[UUID] = frozenset(),
) -> ContextRecallCheckpoint | None:
    """Exactly one ACTIVE or none; never a historical fallback (handoff 14
    SS 33/38/103/104). Unsupported statements are omitted; an unsupported or
    missing ``current_state`` means no current checkpoint."""

    if not active:
        return None
    if len(active) > 1:
        # Violated single-ACTIVE invariant: fail closed -- never "newest wins".
        if not signals.quiet:
            _logger.error(
                "context_recall_invariant_violation",
                owner_kind="checkpoint_stream",
                reason="multiple_active_checkpoints",
                checkpoint_ids=[str(item.checkpoint_id) for item in active],
            )
        signals.warnings.update(
            {ContextRecallWarning.AMBIGUOUS_CURRENT_TRUTH, ContextRecallWarning.PARTIAL_CONTEXT}
        )
        return None
    checkpoint = active[0]
    if checkpoint.current_state is None:
        # CTX-005 invalidates a checkpoint that loses current_state: an ACTIVE one
        # without it is an invariant failure. Expose nothing, repair nothing.
        if not signals.quiet:
            _logger.error(
                "context_recall_invariant_violation",
                owner_kind="checkpoint",
                owner_id=str(checkpoint.checkpoint_id),
                reason="active_checkpoint_without_current_state",
            )
        signals.warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        return None

    excluded = False

    def keep(statement: ContextStatementResult | None) -> ContextStatementResult | None:
        nonlocal excluded
        if statement is None:
            return None
        if _usable(
            "context_statement",
            statement.statement_id,
            statement.support,
            declared=statement.statement_id in declared_statement_ids,
            log=not signals.quiet,
        ):
            return statement
        excluded = True
        return None

    def keep_all(statements: Iterable[ContextStatementResult]) -> list[ContextStatementResult]:
        kept: list[ContextStatementResult] = []
        for statement in statements:
            if keep(statement) is not None:
                kept.append(statement)
        return kept

    current_state = keep(checkpoint.current_state)
    if current_state is None:
        signals.evidence_unavailable()
        return None
    result = ContextRecallCheckpoint(
        **{
            **dict(checkpoint),
            "objective": keep(checkpoint.objective),
            "current_state": current_state,
            "approved_decisions": keep_all(checkpoint.approved_decisions),
            "pending_work": keep_all(checkpoint.pending_work),
            "open_items": keep_all(checkpoint.open_items),
            "blockers": keep_all(checkpoint.blockers),
            "next_action": keep(checkpoint.next_action),
        }
    )
    if excluded:
        signals.evidence_unavailable()
    return result


def select_episodes(
    candidates: Sequence[EpisodeCandidate], limit: int, signals: _Signals
) -> list[ContextRecallEpisode]:
    """First ``limit`` usable candidates in source order. An unsupported
    Episode ranked inside the selection range is excluded and signalled."""

    selected: list[ContextRecallEpisode] = []
    for candidate in candidates:
        if len(selected) >= limit:
            break
        if not _usable(
            "episode",
            candidate.episode.episode_id,
            candidate.episode.support,
            declared=candidate.support_declared,
            log=not signals.quiet,
        ):
            signals.evidence_unavailable()
            continue
        selected.append(
            ContextRecallEpisode(episode=candidate.episode, relevance=candidate.relevance)
        )
    return selected


def _history_eligibility(
    owner_kind: str, owner_id: UUID, support: SupportResult | None, *, declared: bool
) -> tuple[bool, bool]:
    """``(keep, insufficient)`` for explicitly historical content (handoff 15
    SS 86/144, Contract SS 51.1): remaining content whose *ordinary* support is
    currently insufficient may be shown, with its Evidence availability, but a
    declared support without links or a post-Forget invariant violation is
    excluded and logged (ids only) -- history is never a Forget escape hatch."""

    if support is None:
        if declared:
            _logger.error(
                "context_recall_invariant_violation",
                owner_kind=owner_kind,
                owner_id=str(owner_id),
                reason="declared_support_without_links",
            )
            return False, False
        return True, False
    if is_supported(support):
        return True, False
    if violates_forget_invariant(support):
        _logger.error(
            "context_recall_invariant_violation",
            owner_kind=owner_kind,
            owner_id=str(owner_id),
            reason="forgotten_evidence_not_propagated",
        )
        return False, False
    return True, True


def chronological(episodes: Iterable[ContextRecallEpisode]) -> list[ContextRecallEpisode]:
    """``occurred_at DESC, episode_id ASC`` (handoff 15 SS 39/158)."""

    by_id = sorted(episodes, key=lambda item: item.episode.episode_id)
    return sorted(by_id, key=lambda item: cast(datetime, item.episode.occurred_at), reverse=True)


def select_history_episodes(
    candidates: Sequence[EpisodeCandidate], limit: int, signals: _Signals
) -> list[ContextRecallEpisode]:
    """``history``: the first ``limit`` eligible candidates in source order
    (semantic candidate selection, or the chronological primitive), then a
    chronological final ordering -- relevance and chronology are never mixed
    into one score (handoff 15 SS 39/40)."""

    selected: list[ContextRecallEpisode] = []
    for candidate in candidates:
        if len(selected) >= limit:
            break
        keep, insufficient = _history_eligibility(
            "episode",
            candidate.episode.episode_id,
            candidate.episode.support,
            declared=candidate.support_declared,
        )
        if not keep:
            signals.evidence_unavailable()
            continue
        if insufficient:
            signals.warnings.add(ContextRecallWarning.EVIDENCE_UNAVAILABLE)
        selected.append(
            ContextRecallEpisode(episode=candidate.episode, relevance=candidate.relevance)
        )
    return chronological(selected)


def select_historical_checkpoints(
    candidates: Sequence[ContextCheckpointResult],
    signals: _Signals,
    declared_statement_ids: frozenset[UUID] = frozenset(),
    *,
    max_statements: int,
) -> list[ContextRecallHistoricalCheckpoint]:
    """SUPERSEDED/INVALIDATED revisions with only their remaining statements
    (never rebuilt). A revision whose eligible statements exceed the internal
    bound is omitted whole and signalled (never partially rebuilt)."""

    selected: list[ContextRecallHistoricalCheckpoint] = []
    for checkpoint in candidates:

        def keep(statement: ContextStatementResult | None) -> ContextStatementResult | None:
            if statement is None:
                return None
            eligible, insufficient = _history_eligibility(
                "context_statement",
                statement.statement_id,
                statement.support,
                declared=statement.statement_id in declared_statement_ids,
            )
            if not eligible:
                signals.evidence_unavailable()
                return None
            if insufficient:
                signals.warnings.add(ContextRecallWarning.EVIDENCE_UNAVAILABLE)
            return statement

        def keep_all(
            statements: Iterable[ContextStatementResult],
        ) -> list[ContextStatementResult]:
            return [kept for kept in (keep(statement) for statement in statements) if kept]

        result = ContextRecallHistoricalCheckpoint(
            **{
                **dict(checkpoint),
                "objective": keep(checkpoint.objective),
                "current_state": keep(checkpoint.current_state),
                "approved_decisions": keep_all(checkpoint.approved_decisions),
                "pending_work": keep_all(checkpoint.pending_work),
                "open_items": keep_all(checkpoint.open_items),
                "blockers": keep_all(checkpoint.blockers),
                "next_action": keep(checkpoint.next_action),
            }
        )
        if checkpoint_statement_count(result) > max_statements:
            signals.warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
            _logger.warning(
                "context_recall_budget_omission",
                historical_checkpoint_id=str(checkpoint.checkpoint_id),
                checkpoint_statements_exceeded=True,
            )
            continue
        selected.append(result)
    return selected


def _statement_supports(checkpoint: ContextCheckpointResult | None) -> list[SupportResult | None]:
    if checkpoint is None:
        return []
    return [
        statement.support
        for statement in (
            checkpoint.objective,
            checkpoint.current_state,
            *checkpoint.approved_decisions,
            *checkpoint.pending_work,
            *checkpoint.open_items,
            *checkpoint.blockers,
            checkpoint.next_action,
        )
        if statement is not None
    ]


def returned_supports(
    checkpoint: ContextCheckpointResult | None,
    episodes: Sequence[ContextRecallEpisode],
    historical: Sequence[ContextCheckpointResult] = (),
) -> list[SupportResult | None]:
    """The declared supports of every *returned* contextual resource."""

    supports = _statement_supports(checkpoint)
    for item in historical:
        supports.extend(_statement_supports(item))
    supports.extend(item.episode.support for item in episodes)
    return supports


CONTEXT_RECALL_MAX_EVIDENCE_OWNERS = 256
"""Internal hard cap on the owner links serialized across ``evidence[]`` (amended
Contract SS 48): no Evidence identity can acquire unbounded owners. With at most
``CONTEXT_RECALL_MAX_EVIDENCE`` identities this stays far below the byte budget."""

type OwnerKey = tuple[str, str]
type IdentityKey = tuple[str, str]


def _canonical(evidence: EvidenceResult) -> IdentityKey:
    return (evidence.source_type.value, str(evidence.source_id))


def returned_owners(
    checkpoint: ContextCheckpointResult | None,
    episodes: Sequence[ContextRecallEpisode],
    historical: Sequence[ContextCheckpointResult] = (),
) -> list[tuple[str, UUID, SupportResult]]:
    """``(owner_type, owner_id, support)`` of every returned resource that
    declares support, in deterministic ``(owner_type, owner_id)`` order."""

    owners: list[tuple[str, UUID, SupportResult]] = []
    for item in (checkpoint, *historical):
        if item is None:
            continue
        for statement in (
            item.objective,
            item.current_state,
            *item.approved_decisions,
            *item.pending_work,
            *item.open_items,
            *item.blockers,
            item.next_action,
        ):
            if statement is not None and statement.support is not None:
                owners.append(("context_statement", statement.statement_id, statement.support))
    owners.extend(
        ("episode", entry.episode.episode_id, entry.episode.support)
        for entry in episodes
        if entry.episode.support is not None
    )
    return sorted(owners, key=lambda owner: (owner[0], str(owner[1])))


def _evidence_items(
    identities: Mapping[IdentityKey, EvidenceResult],
    owners: Mapping[IdentityKey, Mapping[OwnerKey, SupportMode]],
) -> list[ContextRecallEvidence]:
    return [
        ContextRecallEvidence(
            **dict(identities[identity]),
            owners=[
                ContextRecallEvidenceOwner(
                    owner_type=cast(Literal["context_statement", "episode"], owner[0]),
                    owner_id=UUID(owner[1]),
                    support_mode=mode,
                )
                for owner, mode in sorted(owners[identity].items())
            ],
        )
        for identity in sorted(identities)
    ]


def aggregate_evidence(
    checkpoint: ContextRecallCheckpoint | None,
    episodes: Sequence[ContextRecallEpisode],
    limit: int,
    historical: Sequence[ContextRecallHistoricalCheckpoint] = (),
    *,
    max_owner_edges: int = CONTEXT_RECALL_MAX_EVIDENCE_OWNERS,
) -> tuple[list[ContextRecallEvidence], bool]:
    """resume/context/history: the de-duplicated canonical Evidence identities
    of the *returned* resources, in canonical ``(source_type, source_id)``
    order, at most ``limit`` (unchanged SM-CTX-006 selection), each with its
    direct owners. Returns ``(items, owners_truncated)``. Identity and current
    availability only -- never content."""

    identities: dict[IdentityKey, EvidenceResult] = {}
    links: list[tuple[IdentityKey, OwnerKey, SupportMode]] = []
    for owner_type, owner_id, support in returned_owners(checkpoint, episodes, historical):
        for evidence in support.evidence:
            identities.setdefault(_canonical(evidence), evidence)
            links.append((_canonical(evidence), (owner_type, str(owner_id)), support.mode))
    kept = dict(sorted(identities.items())[:limit])
    owners: dict[IdentityKey, dict[OwnerKey, SupportMode]] = {key: {} for key in kept}
    count, truncated = 0, False
    for identity, owner, mode in sorted(links):
        if identity not in kept or owner in owners[identity]:
            continue
        if count >= max_owner_edges:
            truncated = True
            continue
        owners[identity][owner] = mode
        count += 1
    return _evidence_items(kept, owners), truncated


def trace_evidence(
    checkpoint: ContextRecallCheckpoint | None,
    episodes: Sequence[ContextRecallEpisode],
    trace: EvidenceTrace,
    limit: int,
    *,
    max_owner_edges: int = CONTEXT_RECALL_MAX_EVIDENCE_OWNERS,
) -> tuple[list[ContextRecallEvidence], bool]:
    """``explain`` + ``include_evidence=true``: the bounded causal Evidence trace
    (amended Contract SS 48/49, handoff 15 SS 15/21). Breadth-first from the
    returned resources (deterministic order), following only persisted links:
    an AVAILABLE Episode Evidence is expanded with its own support from the
    snapshot trace -- even when that Episode is not returned in ``episodes[]``
    (identity only, never its payload). An edge is kept only if its owner is
    already part of the kept trace, so every selected edge is connected to a
    returned root; descendants of a dropped edge are never shown. Returns
    ``(items, partial)``: ``partial`` when ``limit`` identities, the owner-link
    cap, a traversal bound (``trace.cut``) or a corrupt cycle prevented the
    complete reachable trace, or an Episode declares support whose persisted
    links are missing -- never presented as complete."""

    identities: dict[IdentityKey, EvidenceResult] = {}
    owners: dict[IdentityKey, dict[OwnerKey, SupportMode]] = {}
    returned_episodes = {entry.episode.episode_id for entry in episodes}
    expanded: set[UUID] = set(returned_episodes)
    queue: deque[tuple[str, UUID, SupportResult]] = deque(returned_owners(checkpoint, episodes))
    count, partial = 0, trace.cycle
    while queue:
        owner_type, owner_id, support = queue.popleft()
        owner = (owner_type, str(owner_id))
        for evidence in sorted(support.evidence, key=_canonical):
            identity = _canonical(evidence)
            if owner in owners.get(identity, {}):
                continue
            if identity not in identities and len(identities) >= limit:
                partial = True
                continue
            if count >= max_owner_edges:
                partial = True
                continue
            identities.setdefault(identity, evidence)
            owners.setdefault(identity, {})[owner] = support.mode
            count += 1
            target = evidence.source_id
            if (
                evidence.source_type is not EvidenceSourceType.EPISODE
                or evidence.availability is not EvidenceAvailability.AVAILABLE
                or target in expanded
            ):
                continue
            expanded.add(target)
            if target in trace.cut or target not in trace.supports:
                partial = True  # reached but not expanded: never claim completeness
                continue
            if target in trace.declared_without_links:
                # Declared dependency without persisted links: an invariant
                # violation, never a legitimate leaf. Nothing is invented.
                partial = True
                continue
            child = trace.supports[target]
            if child is not None:
                queue.append(("episode", target, child))
    return _evidence_items(identities, owners), partial


def knowledge_references(
    hits: Sequence[KnowledgeHit],
    supports: Iterable[SupportResult | None],
    locations: Mapping[EvidenceKey, KnowledgeLocation],
    datasets: Sequence[DatasetSelection],
    limit: int,
) -> list[ContextRecallKnowledgeReference]:
    """Knowledge references of explicitly selected datasets only (handoff 15
    SS 54/62-66/114-118). Retrieval hits keep their local rank order and are
    flagged ``evidence_backed`` when a returned resource explicitly declares
    that chunk as Evidence; AVAILABLE Source/Document/Chunk Evidence of a
    selected dataset that was not retrieved follows as an Evidence-only
    reference (no rank, no score), in stable identity order. Evidence of a
    non-selected dataset stays in the Evidence section only. Without datasets:
    nothing."""

    if not datasets:
        return []
    slugs = {item.dataset_id: item.slug for item in datasets}
    backed: dict[EvidenceKey, KnowledgeLocation] = {}
    for support in supports:
        if support is None:
            continue
        for evidence in support.evidence:
            if (
                evidence.source_type not in KNOWLEDGE_EVIDENCE_TYPES
                or evidence.availability is not EvidenceAvailability.AVAILABLE
            ):
                continue
            key = EvidenceKey(evidence.source_type, evidence.source_id)
            location = locations.get(key)
            if location is not None and location.dataset_id in slugs:
                backed[key] = location
    backed_chunks = {
        location.chunk_id
        for key, location in backed.items()
        if key.source_type is EvidenceSourceType.CHUNK
    }
    references = [
        ContextRecallKnowledgeReference(
            dataset_id=hit.dataset_id,
            dataset_slug=slugs[hit.dataset_id],
            source_id=hit.source_id,
            document_id=hit.document_id,
            chunk_id=hit.chunk_id,
            rank=hit.rank,
            relevance=hit.relevance,
            evidence_backed=hit.chunk_id in backed_chunks,
        )
        for hit in hits
        if hit.dataset_id in slugs
    ]
    retrieved = {hit.chunk_id for hit in hits}
    evidence_only = sorted(
        (
            location
            for key, location in backed.items()
            if not (key.source_type is EvidenceSourceType.CHUNK and location.chunk_id in retrieved)
        ),
        key=lambda loc: (str(loc.source_id), str(loc.document_id or ""), str(loc.chunk_id or "")),
    )
    references.extend(
        ContextRecallKnowledgeReference(
            dataset_id=location.dataset_id,
            dataset_slug=slugs[location.dataset_id],
            source_id=location.source_id,
            document_id=location.document_id,
            chunk_id=location.chunk_id,
            rank=None,
            relevance=None,
            evidence_backed=True,
        )
        for location in evidence_only
    )
    return references[:limit]


def checkpoint_statement_count(checkpoint: ContextCheckpointResult) -> int:
    """Statements of every section of a (eligibility-filtered) checkpoint."""

    singles = (checkpoint.objective, checkpoint.current_state, checkpoint.next_action)
    return sum(statement is not None for statement in singles) + sum(
        len(section)
        for section in (
            checkpoint.approved_decisions,
            checkpoint.pending_work,
            checkpoint.open_items,
            checkpoint.blockers,
        )
    )


def serialized_bundle_bytes(bundle: ProjectContextBundle) -> int:
    """UTF-8 size of the bundle exactly as the API serializes it (compact JSON)."""

    return len(bundle.model_dump_json(by_alias=True).encode("utf-8"))


def _prefix[T](items: Sequence[T], fits: Callable[[list[T]], bool]) -> list[T]:
    kept: list[T] = []
    for item in items:
        if not fits([*kept, item]):
            break
        kept.append(item)
    return kept


def compose_bundle(
    request: ContextRecallRequest,
    limits: ContextRecallLimits,
    snapshot: SourceSnapshot,
    *,
    semantic_degraded: bool,
    max_bundle_bytes: int = CONTEXT_RECALL_MAX_BUNDLE_BYTES,
    max_checkpoint_statements: int = CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS,
) -> ProjectContextBundle:
    """Pure, deterministic per-intent composition (handoff 15 SS 8/110-113).
    Source-specific collections, never one cross-type ranking; every source
    bounded independently and the whole bundle bounded by the byte budget."""

    signals = _Signals()
    history = request.intent is ContextRecallIntent.HISTORY
    explain = request.intent is ContextRecallIntent.EXPLAIN
    if semantic_degraded:
        # One cause, one warning; every affected source named once (SS 71).
        signals.warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        if not history:
            signals.degraded.add(ContextRecallSource.MEMORIES)
        signals.degraded.add(ContextRecallSource.EPISODES)
        if snapshot.datasets:
            signals.degraded.add(ContextRecallSource.KNOWLEDGE)

    checkpoint = select_current_checkpoint(
        snapshot.active_checkpoints, signals, snapshot.declared_statement_ids
    )
    memories: list[ContextRecallMemory]
    historical: list[ContextRecallHistoricalCheckpoint]
    if history:
        memories = []  # current truth != historical timeline (SS 42)
        episodes = select_history_episodes(snapshot.episodes, limits.episodes, signals)
        historical = select_historical_checkpoints(
            snapshot.historical_checkpoints[: limits.historical_checkpoints],
            signals,
            snapshot.declared_statement_ids,
            max_statements=max_checkpoint_statements,
        )
    else:
        memories = [
            ContextRecallMemory(memory=candidate.memory, relevance=candidate.relevance)
            for candidate in snapshot.memories[: limits.memories]
        ]
        episodes = select_episodes(snapshot.episodes, limits.episodes, signals)
        historical = []  # lineage is never explanation (SS 82)
    hits = list(snapshot.knowledge_hits[: limits.knowledge]) if snapshot.datasets else []

    def build(
        current: ContextRecallCheckpoint | None,
        kept_memories: list[ContextRecallMemory],
        kept_episodes: list[ContextRecallEpisode],
        kept_historical: list[ContextRecallHistoricalCheckpoint],
        kept_hits: list[KnowledgeHit],
        *,
        assume_omission: bool,
    ) -> ProjectContextBundle:
        warnings = set(signals.warnings)
        if current is None:
            warnings.add(ContextRecallWarning.NO_CURRENT_CHECKPOINT)
        if assume_omission:
            warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        supports = returned_supports(current, kept_episodes, kept_historical)
        evidence: list[ContextRecallEvidence] = []
        if request.include_evidence:
            if explain and snapshot.trace is not None:
                evidence, partial = trace_evidence(
                    current, kept_episodes, snapshot.trace, limits.evidence
                )
            else:
                evidence, partial = aggregate_evidence(
                    current, kept_episodes, limits.evidence, kept_historical
                )
            if partial:
                # A bound (identities, owner links, traversal) or a corrupt
                # cycle cut the reachable trace: never claim it complete.
                warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        return ProjectContextBundle(
            scope=request.scope,
            context_key=request.context_key,
            intent=request.intent,
            continuity_available=current is not None,
            current_checkpoint=current,
            current_memories=kept_memories,
            episodes=kept_episodes,
            historical_checkpoints=kept_historical,
            knowledge_references=knowledge_references(
                kept_hits,
                supports,
                snapshot.knowledge_locations,
                snapshot.datasets,
                limits.knowledge,
            ),
            evidence=evidence,
            warnings=[warning for warning in ContextRecallWarning if warning in warnings],
            degraded_sources=[
                source for source in ContextRecallSource if source in signals.degraded
            ],
        )

    statements_fit = (
        checkpoint is None or checkpoint_statement_count(checkpoint) <= max_checkpoint_statements
    )
    complete = build(checkpoint, memories, episodes, historical, hits, assume_omission=False)
    if statements_fit and serialized_bundle_bytes(complete) <= max_bundle_bytes:
        return complete

    # Over an operational bound: deterministic greedy composition in priority
    # order (checkpoint, memories, Episodes, historical checkpoints, Knowledge
    # hits), each source keeping a prefix of its own ranking. Evidence and
    # Evidence-backed Knowledge references are derived from the kept resources
    # inside every trial. Every trial is measured *with* PARTIAL_CONTEXT, which
    # the final bundle carries whenever something was omitted, so an accepted
    # resource always fits the final bundle. Nothing is ever truncated. This is
    # healthy operational truncation: never reported in degraded_sources.
    def fits(
        current: ContextRecallCheckpoint | None,
        kept_memories: list[ContextRecallMemory],
        kept_episodes: list[ContextRecallEpisode],
        kept_historical: list[ContextRecallHistoricalCheckpoint],
        kept_hits: list[KnowledgeHit],
    ) -> bool:
        trial = build(
            current, kept_memories, kept_episodes, kept_historical, kept_hits, assume_omission=True
        )
        return serialized_bundle_bytes(trial) <= max_bundle_bytes

    checkpoint_omitted = False
    if checkpoint is not None and (not statements_fit or not fits(checkpoint, [], [], [], [])):
        checkpoint_omitted = True
        checkpoint = None  # omitted whole: continuity_available=false, NO_CURRENT_CHECKPOINT
    kept_memories = _prefix(memories, lambda xs: fits(checkpoint, xs, [], [], []))
    kept_episodes = _prefix(episodes, lambda xs: fits(checkpoint, kept_memories, xs, [], []))
    kept_historical = _prefix(
        historical, lambda xs: fits(checkpoint, kept_memories, kept_episodes, xs, [])
    )
    kept_hits = _prefix(
        hits, lambda xs: fits(checkpoint, kept_memories, kept_episodes, kept_historical, xs)
    )
    omitted = {
        "omitted_memories": len(memories) - len(kept_memories),
        "omitted_episodes": len(episodes) - len(kept_episodes),
        "omitted_historical_checkpoints": len(historical) - len(kept_historical),
        "omitted_knowledge_hits": len(hits) - len(kept_hits),
    }
    if checkpoint_omitted or any(omitted.values()):
        signals.warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        _logger.warning(
            "context_recall_budget_omission",
            checkpoint_omitted=checkpoint_omitted,
            checkpoint_statements_exceeded=not statements_fit,
            max_bundle_bytes=max_bundle_bytes,
            **omitted,
        )
    bundle = build(
        checkpoint,
        kept_memories,
        kept_episodes,
        kept_historical,
        kept_hits,
        assume_omission=False,
    )
    size = serialized_bundle_bytes(bundle)
    if size > max_bundle_bytes:
        # Only reachable when the empty bundle itself exceeds the bound (a
        # misconfigured bound): fail rather than return an oversized response.
        _logger.error(
            "context_recall_budget_exceeded", size=size, max_bundle_bytes=max_bundle_bytes
        )
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="The contextual recall response exceeds its operational bound.",
        )
    return bundle


EXPLAIN_TRACE_MAX_REREADS = 2
"""explain: bounded re-reads of the Evidence trace inside the same snapshot when
the byte budget dropped selected roots *and* the trace was cut by a traversal
bound, so that omitted roots' ancestry does not consume capacity of the kept
ones. Each re-read can only shrink the root set."""


def select_trace_roots(
    snapshot: SourceSnapshot, limits: ContextRecallLimits
) -> tuple[ContextRecallCheckpoint | None, list[ContextRecallEpisode]]:
    """The explain resources that can be returned: the *same* deterministic
    selection ``compose_bundle`` applies (eligible current checkpoint statements,
    the first ``limits.episodes`` eligible Episodes) -- never the whole candidate
    window. Quiet: the authoritative composition logs violations once."""

    quiet = _Signals(quiet=True)
    checkpoint = select_current_checkpoint(
        snapshot.active_checkpoints, quiet, snapshot.declared_statement_ids
    )
    return checkpoint, select_episodes(snapshot.episodes, limits.episodes, quiet)


def trace_root_keys(
    checkpoint: ContextRecallCheckpoint | None, episodes: Sequence[ContextRecallEpisode]
) -> frozenset[OwnerKey]:
    owners = returned_owners(checkpoint, episodes)
    return frozenset((owner_type, str(owner_id)) for owner_type, owner_id, _ in owners)


async def compose_explain_bundle(
    uow: PostgresUnitOfWork,
    request: ContextRecallRequest,
    limits: ContextRecallLimits,
    snapshot: SourceSnapshot,
    *,
    semantic_degraded: bool,
) -> tuple[SourceSnapshot, ProjectContextBundle]:
    """explain + include_evidence=true, inside the caller's read-only snapshot:
    the bounded trace is read from the *selected* roots only (amended Contract
    SS 48: "a partir dos recursos retornados"), so unreturned candidates never
    consume traversal capacity. If the byte budget then drops selected roots
    while the trace was cut, the trace is re-read from the kept roots (at most
    ``EXPLAIN_TRACE_MAX_REREADS`` times; such a bundle already carries
    ``PARTIAL_CONTEXT`` for the omission)."""

    checkpoint, episodes = select_trace_roots(snapshot, limits)
    read_keys = trace_root_keys(checkpoint, episodes)
    trace = await read_evidence_trace(uow, returned_supports(checkpoint, episodes))
    snapshot = replace(snapshot, trace=trace)
    bundle = compose_bundle(request, limits, snapshot, semantic_degraded=semantic_degraded)
    for _ in range(EXPLAIN_TRACE_MAX_REREADS):
        if not trace.cut:
            break
        kept_keys = trace_root_keys(bundle.current_checkpoint, bundle.episodes)
        if kept_keys == read_keys:
            break
        read_keys = kept_keys
        trace = await read_evidence_trace(
            uow, returned_supports(bundle.current_checkpoint, bundle.episodes)
        )
        snapshot = replace(snapshot, trace=trace)
        bundle = compose_bundle(request, limits, snapshot, semantic_degraded=semantic_degraded)
    if trace.declared_without_links:
        # Ids only, never content; nothing is repaired (the Episodes stay as
        # persisted) and the trace never claims completeness through them.
        _logger.error(
            "context_recall_invariant_violation",
            owner_kind="episode",
            owner_ids=sorted(str(episode_id) for episode_id in trace.declared_without_links),
            reason="declared_support_without_links",
        )
    return snapshot, bundle


# --- service -------------------------------------------------------------------------


class ContextualRecallService:
    """``POST /api/v1/context/recall`` for every Contract v1 intent."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
        knowledge_retriever: ContextualKnowledgeRetriever | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )
        self._knowledge_retriever = knowledge_retriever or ContextualKnowledgeRetriever(
            rrf_k=settings.recall_rrf_k
        )

    async def recall(self, request: ContextRecallRequest) -> ProjectContextBundle:
        limits = request.limits.effective()
        # The single provider attempt of this request, outside any transaction;
        # shared by Cognitive Memory, Episodes and Knowledge vector retrieval.
        query_embedding = await self._embed_query_once(request.query)
        try:
            snapshot, bundle = await self._read_and_compose(
                request, query_embedding=query_embedding, limits=limits, as_of=utc_now()
            )
        except (OperationalError, InterfaceError, OSError, PoolTimeoutError) as exc:
            # The authoritative store is unavailable -- unreachable, or no pooled
            # connection could be acquired in time (sqlalchemy.exc.TimeoutError,
            # pool exhaustion): fail, never a fabricated bundle (Contract SS 52 ->
            # 503, handoff 14 SS 61, handoff 15 SS 72).
            raise DependencyUnavailableError("PostgreSQL is unavailable.") from exc
        if snapshot.trace is not None and snapshot.trace.cycle:
            _logger.error(
                "context_recall_invariant_violation",
                owner_kind="episode_evidence_graph",
                reason="evidence_cycle",
            )
        _logger.info(
            "context_recall",
            scope=bundle.scope,
            context_key=bundle.context_key,
            intent=bundle.intent.value,
            degraded=bool(bundle.degraded_sources),
            continuity_available=bundle.continuity_available,
            memory_count=len(bundle.current_memories),
            episode_count=len(bundle.episodes),
            historical_checkpoint_count=len(bundle.historical_checkpoints),
            dataset_count=len(snapshot.datasets),
            knowledge_reference_count=len(bundle.knowledge_references),
            evidence_count=len(bundle.evidence),
            traced_episode_count=0 if snapshot.trace is None else len(snapshot.trace.supports),
            warnings=[warning.value for warning in bundle.warnings],
        )
        return bundle

    async def _embed_query_once(self, query: str) -> list[float] | None:
        """Exactly one provider attempt, outside any transaction. ``None``
        means semantic retrieval is unavailable for this request (no retry)."""

        try:
            embeddings = await self._embedding_client.embed_texts([query])
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never 500
            _logger.warning(
                "context_recall_embedding_unavailable", exception_type=type(exc).__name__
            )
            return None
        if len(embeddings) != 1 or len(embeddings[0]) != self._settings.embedding_dimensions:
            _logger.warning("context_recall_embedding_unavailable", exception_type="dimension")
            return None
        if not is_valid_query_embedding(embeddings[0]):
            # Same explicit degraded fallback as an unavailable provider; no retry.
            _logger.warning("context_recall_embedding_unavailable", exception_type="invalid_vector")
            return None
        return embeddings[0]

    async def _read_and_compose(
        self,
        request: ContextRecallRequest,
        *,
        query_embedding: list[float] | None,
        limits: ContextRecallLimits,
        as_of: datetime,
    ) -> tuple[SourceSnapshot, ProjectContextBundle]:
        """One short read-only unit of work (rolled back, never committed).
        Public shapes are built while the session is open (rollback expires
        loaded ORM instances). explain with Evidence composes inside the same
        snapshot, because its trace is read from the selected roots."""

        scope, context_key = request.scope, request.context_key
        history = request.intent is ContextRecallIntent.HISTORY
        episode_window = min(
            limits.episodes * EPISODE_CANDIDATE_WINDOW_FACTOR, EPISODE_CANDIDATE_WINDOW_MAX
        )
        async with self._unit_of_work_factory() as uow:
            # One consistent snapshot for every source, before the first read
            # (a concurrent Forget can never be half-observed).
            await uow.begin_read_only_snapshot()
            # Explicit datasets are validated first, in the same snapshot: a
            # missing or non-ACTIVE one fails the request (never ignored, never
            # 'main', never created).
            datasets = (
                await self._knowledge_retriever.resolve_datasets(uow, request.datasets)
                if request.datasets
                else []
            )
            active_rows = await uow.context_checkpoints.list_active(
                scope=scope, context_key=context_key, limit=2
            )
            active: list[ContextCheckpointResult] = []
            statement_modes: dict[UUID, object] = {}
            if active_rows:
                active, modes = await hydrate_checkpoints_with_support_modes(uow, active_rows)
                statement_modes.update(modes)
            historical: list[ContextCheckpointResult] = []
            if history:
                history_rows = await uow.context_checkpoints.list_history(
                    scope=scope, context_key=context_key, limit=limits.historical_checkpoints
                )
                if history_rows:
                    historical, modes = await hydrate_checkpoints_with_support_modes(
                        uow, history_rows
                    )
                    statement_modes.update(modes)
            declared_statement_ids = frozenset(
                statement_id for statement_id, mode in statement_modes.items() if mode is not None
            )

            memories: list[MemoryCandidate] = []
            if not history:
                memories = await self._read_memories(
                    uow,
                    scope=scope,
                    query_embedding=query_embedding,
                    as_of=as_of,
                    limit=limits.memories,
                )
            episodes = await self._read_episodes(
                uow,
                scope=scope,
                context_key=context_key,
                query_embedding=query_embedding,
                window=episode_window,
                history=history,
            )

            knowledge_hits: list[KnowledgeHit] = []
            knowledge_locations: dict[EvidenceKey, KnowledgeLocation] = {}
            if datasets:
                knowledge_hits = await self._knowledge_retriever.retrieve(
                    uow,
                    datasets=datasets,
                    query=request.query,
                    query_embedding=query_embedding,
                    limit=limits.knowledge,
                )
                candidate_supports = [
                    *(s for c in active for s in _statement_supports(c)),
                    *(s for c in historical for s in _statement_supports(c)),
                    *(candidate.episode.support for candidate in episodes),
                ]
                knowledge_locations = await self._knowledge_locations(
                    uow, candidate_supports, datasets
                )

            snapshot = SourceSnapshot(
                active_checkpoints=active,
                memories=memories,
                episodes=episodes,
                declared_statement_ids=declared_statement_ids,
                historical_checkpoints=historical,
                datasets=datasets,
                knowledge_hits=knowledge_hits,
                knowledge_locations=knowledge_locations,
            )
            semantic_degraded = query_embedding is None
            if request.intent is ContextRecallIntent.EXPLAIN and request.include_evidence:
                # The public causal trace exists only when Evidence is requested
                # (include_evidence=false keeps the SM-CTX-006 output contract).
                # It is read from the selected roots, never the candidate window.
                return await compose_explain_bundle(
                    uow, request, limits, snapshot, semantic_degraded=semantic_degraded
                )
        return snapshot, compose_bundle(
            request, limits, snapshot, semantic_degraded=semantic_degraded
        )

    @staticmethod
    async def _read_memories(
        uow: PostgresUnitOfWork,
        *,
        scope: str,
        query_embedding: list[float] | None,
        as_of: datetime,
        limit: int,
    ) -> list[MemoryCandidate]:
        if query_embedding is not None:
            hits = await uow.memory_items.typed_recall(
                memory_types=CONTEXT_RECALL_MEMORY_TYPES,
                scopes=[scope],
                query_embedding=query_embedding,
                as_of=as_of,
                include_superseded=False,
                min_relevance=None,
                limit=limit,
            )
            return [
                MemoryCandidate(memory_item_result(hit.item, hit.provenance), hit.relevance)
                for hit in hits
            ]
        rows = await uow.memory_items.list_current_for_scope(
            memory_types=CONTEXT_RECALL_MEMORY_TYPES, scope=scope, as_of=as_of, limit=limit
        )
        return [MemoryCandidate(memory_item_result(row.item, row.provenance), None) for row in rows]

    @staticmethod
    async def _read_episodes(
        uow: PostgresUnitOfWork,
        *,
        scope: str,
        context_key: str,
        query_embedding: list[float] | None,
        window: int,
        history: bool,
    ) -> list[EpisodeCandidate]:
        """Semantic candidates when the query embedding exists, otherwise the
        deterministic recency primitive. ``history`` reads ACTIVE and
        INVALIDATED Episodes; every other intent ACTIVE only."""

        rows: Sequence[EpisodeRow | EpisodeSemanticHit]
        if query_embedding is not None:
            search = (
                uow.episodes.historical_semantic_search
                if history
                else uow.episodes.contextual_semantic_search
            )
            rows = await search(
                scope=scope, context_key=context_key, query_embedding=query_embedding, limit=window
            )
        else:
            recent = (
                uow.episodes.list_recent_historical
                if history
                else uow.episodes.list_recent_contextual
            )
            rows = await recent(scope=scope, context_key=context_key, limit=window)
        supports = await hydrate_episode_support(
            uow, {row.episode.id: row.episode.support_mode for row in rows}
        )
        return [
            EpisodeCandidate(
                episode_result(row.episode, row.provenance, supports[row.episode.id]),
                row.relevance if isinstance(row, EpisodeSemanticHit) else None,
                support_declared=row.episode.support_mode is not None,
            )
            for row in rows
        ]

    @staticmethod
    async def _knowledge_locations(
        uow: PostgresUnitOfWork,
        supports: Iterable[SupportResult | None],
        datasets: Sequence[DatasetSelection],
    ) -> dict[EvidenceKey, KnowledgeLocation]:
        keys = sorted(
            {
                EvidenceKey(evidence.source_type, evidence.source_id)
                for support in supports
                if support is not None
                for evidence in support.evidence
                if evidence.source_type in KNOWLEDGE_EVIDENCE_TYPES
                and evidence.availability is EvidenceAvailability.AVAILABLE
            },
            key=lambda key: (key.source_type.value, str(key.source_id)),
        )
        if not keys:
            return {}
        selected = {item.dataset_id for item in datasets}
        locations = await uow.contextual_evidence.knowledge_locations(keys)
        return {key: loc for key, loc in locations.items() if loc.dataset_id in selected}


def is_valid_query_embedding(vector: Sequence[float]) -> bool:
    """A usable query vector, judged as pgvector stores it (float4): every
    component numeric and finite, and a nonzero magnitude (cosine similarity is
    undefined for a zero vector). Values that overflow float4 (become infinite)
    or underflow to an all-zero vector are invalid too."""

    try:
        stored = array("f", vector)
    except (TypeError, OverflowError):
        return False
    return all(math.isfinite(component) for component in stored) and any(stored)


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work


__all__ = [
    "CONTEXT_RECALL_MAX_BUNDLE_BYTES",
    "CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS",
    "CONTEXT_RECALL_MAX_EVIDENCE_OWNERS",
    "CONTEXT_RECALL_MEMORY_TYPES",
    "SUPPORTED_INTENTS",
    "ContextualRecallService",
    "EpisodeCandidate",
    "MemoryCandidate",
    "SourceSnapshot",
    "aggregate_evidence",
    "chronological",
    "knowledge_references",
    "returned_owners",
    "returned_supports",
    "checkpoint_statement_count",
    "compose_bundle",
    "is_valid_query_embedding",
    "select_current_checkpoint",
    "select_episodes",
    "select_historical_checkpoints",
    "select_history_episodes",
    "serialized_bundle_bytes",
    "trace_evidence",
    "violates_forget_invariant",
]
