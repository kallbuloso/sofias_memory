"""Explicit PostgreSQL Unit of Work."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from sofias_memory.infrastructure.postgres.repositories import (
    AgentRepository,
    AgentSessionRepository,
    AgentSkillRepository,
    ChunkRepository,
    CognitiveMemoryIdempotencyRepository,
    ContextCheckpointProvenanceRepository,
    ContextCheckpointRepository,
    ContextCheckpointStatementRepository,
    ContextualEvidenceRepository,
    ContextualMemoryIdempotencyRepository,
    DatasetRepository,
    DocumentRepository,
    EntityMentionRepository,
    EntityRepository,
    EpisodeProvenanceRepository,
    EpisodeRepository,
    FeedbackRepository,
    GraphOutboxRepository,
    MemoryItemRepository,
    MemoryProvenanceRepository,
    PipelineRunRepository,
    PipelineStepRepository,
    QueryRepository,
    RelationEvidenceRepository,
    RelationRepository,
    SessionEntryRepository,
    SessionRepository,
    SkillRepository,
    SkillRevisionRepository,
    SourceRepository,
    SummaryRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory


class PostgresUnitOfWork:
    """Own one AsyncSession and transaction boundary for a write operation."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False
        self._rolled_back = False
        self._datasets: DatasetRepository | None = None
        self._sources: SourceRepository | None = None
        self._summaries: SummaryRepository | None = None
        self._documents: DocumentRepository | None = None
        self._chunks: ChunkRepository | None = None
        self._entities: EntityRepository | None = None
        self._entity_mentions: EntityMentionRepository | None = None
        self._relations: RelationRepository | None = None
        self._relation_evidence: RelationEvidenceRepository | None = None
        self._pipeline_runs: PipelineRunRepository | None = None
        self._pipeline_steps: PipelineStepRepository | None = None
        self._queries: QueryRepository | None = None
        self._feedback: FeedbackRepository | None = None
        self._graph_outbox: GraphOutboxRepository | None = None
        self._sessions: SessionRepository | None = None
        self._session_entries: SessionEntryRepository | None = None
        self._skills: SkillRepository | None = None
        self._skill_revisions: SkillRevisionRepository | None = None
        self._agents: AgentRepository | None = None
        self._agent_skills: AgentSkillRepository | None = None
        self._agent_sessions: AgentSessionRepository | None = None
        self._memory_items: MemoryItemRepository | None = None
        self._memory_provenance: MemoryProvenanceRepository | None = None
        self._cognitive_memory_idempotency: CognitiveMemoryIdempotencyRepository | None = None
        self._episodes: EpisodeRepository | None = None
        self._episode_provenance: EpisodeProvenanceRepository | None = None
        self._contextual_memory_idempotency: ContextualMemoryIdempotencyRepository | None = None
        self._context_checkpoints: ContextCheckpointRepository | None = None
        self._context_checkpoint_statements: ContextCheckpointStatementRepository | None = None
        self._context_checkpoint_provenance: ContextCheckpointProvenanceRepository | None = None
        self._contextual_evidence: ContextualEvidenceRepository | None = None

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("PostgresUnitOfWork is already active")

        session = self._session_factory()
        self._session = session
        self._committed = False
        self._rolled_back = False
        self._datasets = DatasetRepository(session)
        self._sources = SourceRepository(session)
        self._summaries = SummaryRepository(session)
        self._documents = DocumentRepository(session)
        self._chunks = ChunkRepository(session)
        self._entities = EntityRepository(session)
        self._entity_mentions = EntityMentionRepository(session)
        self._relations = RelationRepository(session)
        self._relation_evidence = RelationEvidenceRepository(session)
        self._pipeline_runs = PipelineRunRepository(session)
        self._pipeline_steps = PipelineStepRepository(session)
        self._queries = QueryRepository(session)
        self._feedback = FeedbackRepository(session)
        self._graph_outbox = GraphOutboxRepository(session)
        self._sessions = SessionRepository(session)
        self._session_entries = SessionEntryRepository(session)
        self._skills = SkillRepository(session)
        self._skill_revisions = SkillRevisionRepository(session)
        self._agents = AgentRepository(session)
        self._agent_skills = AgentSkillRepository(session)
        self._agent_sessions = AgentSessionRepository(session)
        self._memory_items = MemoryItemRepository(session)
        self._memory_provenance = MemoryProvenanceRepository(session)
        self._cognitive_memory_idempotency = CognitiveMemoryIdempotencyRepository(session)
        self._episodes = EpisodeRepository(session)
        self._episode_provenance = EpisodeProvenanceRepository(session)
        self._contextual_memory_idempotency = ContextualMemoryIdempotencyRepository(session)
        self._context_checkpoints = ContextCheckpointRepository(session)
        self._context_checkpoint_statements = ContextCheckpointStatementRepository(session)
        self._context_checkpoint_provenance = ContextCheckpointProvenanceRepository(session)
        self._contextual_evidence = ContextualEvidenceRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or (not self._committed and not self._rolled_back):
                await session.rollback()
        finally:
            await session.close()
            self._clear()

    @property
    def datasets(self) -> DatasetRepository:
        if self._datasets is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._datasets

    @property
    def sources(self) -> SourceRepository:
        if self._sources is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._sources

    @property
    def summaries(self) -> SummaryRepository:
        if self._summaries is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._summaries

    @property
    def documents(self) -> DocumentRepository:
        if self._documents is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._documents

    @property
    def chunks(self) -> ChunkRepository:
        if self._chunks is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._chunks

    @property
    def entities(self) -> EntityRepository:
        if self._entities is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._entities

    @property
    def entity_mentions(self) -> EntityMentionRepository:
        if self._entity_mentions is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._entity_mentions

    @property
    def relations(self) -> RelationRepository:
        if self._relations is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._relations

    @property
    def relation_evidence(self) -> RelationEvidenceRepository:
        if self._relation_evidence is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._relation_evidence

    @property
    def pipeline_runs(self) -> PipelineRunRepository:
        if self._pipeline_runs is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._pipeline_runs

    @property
    def pipeline_steps(self) -> PipelineStepRepository:
        if self._pipeline_steps is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._pipeline_steps

    @property
    def queries(self) -> QueryRepository:
        if self._queries is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._queries

    @property
    def feedback(self) -> FeedbackRepository:
        if self._feedback is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._feedback

    @property
    def graph_outbox(self) -> GraphOutboxRepository:
        if self._graph_outbox is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._graph_outbox

    @property
    def sessions(self) -> SessionRepository:
        if self._sessions is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._sessions

    @property
    def session_entries(self) -> SessionEntryRepository:
        if self._session_entries is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._session_entries

    @property
    def skills(self) -> SkillRepository:
        if self._skills is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._skills

    @property
    def skill_revisions(self) -> SkillRevisionRepository:
        if self._skill_revisions is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._skill_revisions

    @property
    def agents(self) -> AgentRepository:
        if self._agents is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._agents

    @property
    def agent_skills(self) -> AgentSkillRepository:
        if self._agent_skills is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._agent_skills

    @property
    def agent_sessions(self) -> AgentSessionRepository:
        if self._agent_sessions is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._agent_sessions

    @property
    def memory_items(self) -> MemoryItemRepository:
        if self._memory_items is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._memory_items

    @property
    def memory_provenance(self) -> MemoryProvenanceRepository:
        if self._memory_provenance is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._memory_provenance

    @property
    def cognitive_memory_idempotency(self) -> CognitiveMemoryIdempotencyRepository:
        if self._cognitive_memory_idempotency is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._cognitive_memory_idempotency

    @property
    def episodes(self) -> EpisodeRepository:
        if self._episodes is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._episodes

    @property
    def episode_provenance(self) -> EpisodeProvenanceRepository:
        if self._episode_provenance is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._episode_provenance

    @property
    def contextual_memory_idempotency(self) -> ContextualMemoryIdempotencyRepository:
        if self._contextual_memory_idempotency is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._contextual_memory_idempotency

    @property
    def context_checkpoints(self) -> ContextCheckpointRepository:
        if self._context_checkpoints is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._context_checkpoints

    @property
    def context_checkpoint_statements(self) -> ContextCheckpointStatementRepository:
        if self._context_checkpoint_statements is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._context_checkpoint_statements

    @property
    def context_checkpoint_provenance(self) -> ContextCheckpointProvenanceRepository:
        if self._context_checkpoint_provenance is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._context_checkpoint_provenance

    @property
    def contextual_evidence(self) -> ContextualEvidenceRepository:
        if self._contextual_evidence is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._contextual_evidence

    def savepoint(self) -> AsyncSessionTransaction:
        """A nested transaction (SAVEPOINT) inside this unit of work.

        Lets one operation of a multi-part write be rolled back on its own
        without discarding -- or poisoning -- the surrounding transaction,
        which is what per-item failure isolation needs when the whole batch
        must still commit exactly once (AGENTS SS 15). Never commits the
        outer transaction: leaving the block normally only releases the
        savepoint.
        """

        return self._require_session().begin_nested()

    async def flush(self) -> None:
        """Synchronize pending authoritative changes without committing them."""

        session = self._require_session()
        await session.flush()

    async def commit(self) -> None:
        session = self._require_session()
        await session.commit()
        self._committed = True
        self._rolled_back = False

    async def rollback(self) -> None:
        session = self._require_session()
        await session.rollback()
        self._rolled_back = True
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._session

    def _clear(self) -> None:
        self._session = None
        self._committed = False
        self._rolled_back = False
        self._datasets = None
        self._sources = None
        self._summaries = None
        self._documents = None
        self._chunks = None
        self._entities = None
        self._entity_mentions = None
        self._relations = None
        self._relation_evidence = None
        self._pipeline_runs = None
        self._pipeline_steps = None
        self._queries = None
        self._feedback = None
        self._graph_outbox = None
        self._sessions = None
        self._session_entries = None
        self._skills = None
        self._skill_revisions = None
        self._agents = None
        self._agent_skills = None
        self._agent_sessions = None
        self._memory_items = None
        self._memory_provenance = None
        self._cognitive_memory_idempotency = None
        self._episodes = None
        self._episode_provenance = None
        self._contextual_memory_idempotency = None
        self._context_checkpoints = None
        self._context_checkpoint_statements = None
        self._context_checkpoint_provenance = None
        self._contextual_evidence = None
