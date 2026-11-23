"""Contextual Recall Core (SM-CTX-006, Contract v1 Contextual Project Memory
SS 37-53, Architecture SS 10/42/61/67/69, handoff 14).

``ContextualRecallService`` is a synchronous, read-only orchestrator over
exactly three first-party sources -- the current ContextCheckpoint, current
project-scoped Cognitive Memory and ACTIVE Episodes -- plus optional Evidence
identities. It owns no corpus, never generates an answer and never mutates
anything: no Query/PipelineRun/PipelineStep/Session/SessionEntry/MemoryItem/
Episode/ContextCheckpoint/graph_outbox row, no LLM, no Knowledge Recall, no
Neo4j. It is deliberately separate from ``services.recall`` (Knowledge
Recall: Query audit, graph, RAG, Session admission).

Flow (handoff 14 SS 56): validate the partial-contract capability (intent,
datasets) -> at most one query embedding, before any PostgreSQL session opens
-> one short read-only unit of work (never committed) reading every source ->
pure, deterministic composition. A failed embedding is never retried: the
bundle degrades to the deterministic bounded fallback with ``PARTIAL_CONTEXT``
and the affected ``degraded_sources`` (never a silent fallback). PostgreSQL
failures propagate -- the database is the authority of the whole bundle.

Eligibility (handoff 14 SS 63, Contract SS 51.1): persistence lifecycle is not
recall eligibility. An ACTIVE Episode or a ContextStatement whose explicit
support is currently insufficient is excluded from the usable context
(``EVIDENCE_UNAVAILABLE`` + ``PARTIAL_CONTEXT``); without an eligible
``current_state`` there is no current checkpoint (``continuity_available=false``,
``NO_CURRENT_CHECKPOINT``) and never a historical fallback. Rows are never
mutated or "repaired" during a read. Collections are ranked per source (no
cross-source score competition) and each is bounded independently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from typing import cast
from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CognitiveMemoryType,
    EvidenceAvailability,
    evaluate_support,
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
    ContextRecallIntent,
    ContextRecallLimits,
    ContextRecallMemory,
    ContextRecallRequest,
    ContextRecallSource,
    ContextRecallWarning,
    ProjectContextBundle,
)
from sofias_memory.schemas.contextual_evidence import SupportResult
from sofias_memory.schemas.episodes import EpisodeResult
from sofias_memory.schemas.memories import MemoryItemResult
from sofias_memory.services.cognitive_memory import EmbeddingClient, memory_item_result
from sofias_memory.services.context_checkpoints import hydrate_checkpoints
from sofias_memory.services.contextual_evidence import hydrate_episode_support, is_supported
from sofias_memory.services.episodes import episode_result

type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]

SUPPORTED_INTENTS = frozenset({ContextRecallIntent.RESUME, ContextRecallIntent.CONTEXT})
"""Implemented in SM-CTX-006; explain/history belong to SM-CTX-007."""

CONTEXT_RECALL_MEMORY_TYPES: tuple[CognitiveMemoryType, ...] = (
    CognitiveMemoryType.PROFILE,
    CognitiveMemoryType.SEMANTIC,
)

EPISODE_CANDIDATE_WINDOW_FACTOR = 3
EPISODE_CANDIDATE_WINDOW_MAX = 150
"""Episodes are read in a bounded candidate window (``limit * 3``, at most 150)
so that excluding currently unsupported Episodes rarely leaves the requested
limit unfilled. Never unbounded."""

_logger = get_logger(__name__)


# --- errors -------------------------------------------------------------------------


def unsupported_partial_contract_error(field_name: str, reason: str) -> SofiasMemoryError:
    """Stable ``INVALID_REQUEST`` (422) for a Contract v1 input this partial
    implementation does not support yet -- never ignored (handoff 14 SS 21/25)."""

    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="This Contextual Recall input is not available in the current implementation.",
        details={"field": field_name, "reason": reason},
    )


def validate_partial_contract(request: ContextRecallRequest) -> None:
    if request.intent not in SUPPORTED_INTENTS:
        raise unsupported_partial_contract_error(
            "intent",
            f"intent '{request.intent.value}' is not available yet; supported: context, resume",
        )
    if request.datasets:
        raise unsupported_partial_contract_error(
            "datasets", "Knowledge Memory composition is not available yet; omit datasets"
        )


# --- source snapshot (read inside one short unit of work) ---------------------------


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory: MemoryItemResult
    relevance: float | None


@dataclass(frozen=True, slots=True)
class EpisodeCandidate:
    episode: EpisodeResult
    relevance: float | None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Everything read from PostgreSQL for one recall, already converted to
    public shapes with *current* Evidence availability. Candidates are in
    their source-specific deterministic order."""

    active_checkpoints: list[ContextCheckpointResult]
    memories: list[MemoryCandidate]
    episodes: list[EpisodeCandidate]


# --- pure composition -----------------------------------------------------------------


@dataclass(slots=True)
class _Signals:
    warnings: set[ContextRecallWarning] = field(default_factory=set)
    degraded: set[ContextRecallSource] = field(default_factory=set)

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


def _usable(owner_kind: str, owner_id: UUID, support: SupportResult | None) -> bool:
    """``support=None`` (no declared dependency) is usable; insufficient support
    is excluded. A post-Forget invariant violation is excluded as well and
    logged (ids only, never content) -- recall never repairs it."""

    if is_supported(support) is not False:
        return True
    if support is not None and violates_forget_invariant(support):
        _logger.error(
            "context_recall_invariant_violation",
            owner_kind=owner_kind,
            owner_id=str(owner_id),
            reason="forgotten_evidence_not_propagated",
        )
    return False


def select_current_checkpoint(
    active: Sequence[ContextCheckpointResult], signals: _Signals
) -> ContextRecallCheckpoint | None:
    """Exactly one ACTIVE or none; never a historical fallback (handoff 14
    SS 33/38/103/104). Unsupported statements are omitted; an unsupported or
    missing ``current_state`` means no current checkpoint."""

    if not active:
        return None
    if len(active) > 1:
        # Violated single-ACTIVE invariant: fail closed -- never "newest wins".
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
        if _usable("context_statement", statement.statement_id, statement.support):
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
        if not _usable("episode", candidate.episode.episode_id, candidate.episode.support):
            signals.evidence_unavailable()
            continue
        selected.append(
            ContextRecallEpisode(episode=candidate.episode, relevance=candidate.relevance)
        )
    return selected


def aggregate_evidence(
    checkpoint: ContextRecallCheckpoint | None,
    episodes: Sequence[ContextRecallEpisode],
    limit: int,
) -> list[ContextRecallEvidence]:
    """De-duplicated canonical Evidence identities of the *returned* resources,
    in canonical ``(source_type, source_id)`` order, at most ``limit``.
    Identity and current availability only -- never content."""

    supports: list[SupportResult | None] = []
    if checkpoint is not None:
        supports.extend(
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
        )
    supports.extend(item.episode.support for item in episodes)
    unique: dict[tuple[str, str], ContextRecallEvidence] = {}
    for support in supports:
        if support is None:
            continue
        for evidence in support.evidence:
            identity = (evidence.source_type.value, str(evidence.source_id))
            if identity not in unique:
                unique[identity] = ContextRecallEvidence(**dict(evidence))
    return [unique[identity] for identity in sorted(unique)][:limit]


def compose_bundle(
    request: ContextRecallRequest,
    limits: ContextRecallLimits,
    snapshot: SourceSnapshot,
    *,
    semantic_degraded: bool,
) -> ProjectContextBundle:
    signals = _Signals()
    if semantic_degraded:
        signals.warnings.add(ContextRecallWarning.PARTIAL_CONTEXT)
        signals.degraded.update({ContextRecallSource.MEMORIES, ContextRecallSource.EPISODES})

    checkpoint = select_current_checkpoint(snapshot.active_checkpoints, signals)
    continuity_available = checkpoint is not None
    if not continuity_available:
        signals.warnings.add(ContextRecallWarning.NO_CURRENT_CHECKPOINT)

    memories = [
        ContextRecallMemory(memory=candidate.memory, relevance=candidate.relevance)
        for candidate in snapshot.memories[: limits.memories]
    ]
    episodes = select_episodes(snapshot.episodes, limits.episodes, signals)
    evidence = (
        aggregate_evidence(checkpoint, episodes, limits.evidence)
        if request.include_evidence
        else []
    )
    return ProjectContextBundle(
        scope=request.scope,
        context_key=request.context_key,
        intent=request.intent,
        continuity_available=continuity_available,
        current_checkpoint=checkpoint,
        current_memories=memories,
        episodes=episodes,
        evidence=evidence,
        warnings=[warning for warning in ContextRecallWarning if warning in signals.warnings],
        degraded_sources=[source for source in ContextRecallSource if source in signals.degraded],
    )


# --- service -------------------------------------------------------------------------


class ContextualRecallService:
    """``POST /api/v1/context/recall`` for the ``resume`` and ``context`` intents."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def recall(self, request: ContextRecallRequest) -> ProjectContextBundle:
        validate_partial_contract(request)  # before any provider call or read
        limits = request.limits.effective()
        query_embedding = await self._embed_query_once(request.query)
        try:
            snapshot = await self._read_sources(
                scope=request.scope,
                context_key=request.context_key,
                query_embedding=query_embedding,
                limits=limits,
                as_of=utc_now(),
            )
        except (OperationalError, InterfaceError, OSError) as exc:
            # The authoritative store is unavailable: fail, never a fabricated
            # bundle (Contract SS 52 -> 503, handoff 14 SS 61).
            raise DependencyUnavailableError("PostgreSQL is unavailable.") from exc
        bundle = compose_bundle(
            request, limits, snapshot, semantic_degraded=query_embedding is None
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
            evidence_count=len(bundle.evidence),
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
        return embeddings[0]

    async def _read_sources(
        self,
        *,
        scope: str,
        context_key: str,
        query_embedding: list[float] | None,
        limits: ContextRecallLimits,
        as_of: datetime,
    ) -> SourceSnapshot:
        """One short read-only unit of work (rolled back, never committed).
        Public shapes are built while the session is open (rollback expires
        loaded ORM instances)."""

        episode_window = min(
            limits.episodes * EPISODE_CANDIDATE_WINDOW_FACTOR, EPISODE_CANDIDATE_WINDOW_MAX
        )
        async with self._unit_of_work_factory() as uow:
            active_rows = await uow.context_checkpoints.list_active(
                scope=scope, context_key=context_key, limit=2
            )
            active = await hydrate_checkpoints(uow, active_rows) if active_rows else []

            memories: list[MemoryCandidate]
            episodes: list[EpisodeCandidate]
            if query_embedding is not None:
                memory_hits = await uow.memory_items.typed_recall(
                    memory_types=CONTEXT_RECALL_MEMORY_TYPES,
                    scopes=[scope],
                    query_embedding=query_embedding,
                    as_of=as_of,
                    include_superseded=False,
                    min_relevance=None,
                    limit=limits.memories,
                )
                memories = [
                    MemoryCandidate(memory_item_result(hit.item, hit.provenance), hit.relevance)
                    for hit in memory_hits
                ]
                episode_hits = await uow.episodes.contextual_semantic_search(
                    scope=scope,
                    context_key=context_key,
                    query_embedding=query_embedding,
                    limit=episode_window,
                )
                episode_supports = await hydrate_episode_support(
                    uow, {hit.episode.id: hit.episode.support_mode for hit in episode_hits}
                )
                episodes = [
                    EpisodeCandidate(
                        episode_result(
                            hit.episode, hit.provenance, episode_supports[hit.episode.id]
                        ),
                        hit.relevance,
                    )
                    for hit in episode_hits
                ]
            else:
                memory_rows = await uow.memory_items.list_current_for_scope(
                    memory_types=CONTEXT_RECALL_MEMORY_TYPES,
                    scope=scope,
                    as_of=as_of,
                    limit=limits.memories,
                )
                memories = [
                    MemoryCandidate(memory_item_result(row.item, row.provenance), None)
                    for row in memory_rows
                ]
                episode_rows = await uow.episodes.list_recent_contextual(
                    scope=scope, context_key=context_key, limit=episode_window
                )
                episode_supports = await hydrate_episode_support(
                    uow, {row.episode.id: row.episode.support_mode for row in episode_rows}
                )
                episodes = [
                    EpisodeCandidate(
                        episode_result(
                            row.episode, row.provenance, episode_supports[row.episode.id]
                        ),
                        None,
                    )
                    for row in episode_rows
                ]
        return SourceSnapshot(active_checkpoints=active, memories=memories, episodes=episodes)


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work


__all__ = [
    "CONTEXT_RECALL_MEMORY_TYPES",
    "SUPPORTED_INTENTS",
    "ContextualRecallService",
    "EpisodeCandidate",
    "MemoryCandidate",
    "SourceSnapshot",
    "aggregate_evidence",
    "compose_bundle",
    "select_current_checkpoint",
    "select_episodes",
    "unsupported_partial_contract_error",
    "validate_partial_contract",
    "violates_forget_invariant",
]
