"""Explicit Improve v1 service for applying persisted feedback weights."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import (
    Dataset,
    Entity,
    EntityMention,
    PipelineRun,
    Relation,
    RelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.entities import (
    EntityDuplicateCandidate,
    EntityEmbeddingCandidate,
)
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.infrastructure.postgres.repositories.relations import RelationEmbeddingCandidate
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import (
    ProjectionCommand,
    entity_delete_command,
    entity_mention_upsert_command,
    entity_upsert_command,
    relation_upsert_command,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.improve import ImproveRequest, ImproveResult
from sofias_memory.services.feedback import (
    ANSWER_TARGET_TYPE,
    REFERENCE_TARGET_TYPE,
    reference_chunk_ids,
)
from sofias_memory.services.graph_maintenance_service import (
    GraphMaintenanceCounts,
    ImportanceComponents,
    has_importance_marker,
    importance_components_from_properties,
    properties_with_importance_marker,
)
from sofias_memory.services.graph_reconciliation_service import (
    GraphReconciliationDiff,
    GraphReconciliationResult,
)
from sofias_memory.services.remember import stable_payload_hash
from sofias_memory.services.summary_rebuild_service import SummaryRebuildCounts

FEEDBACK_WEIGHTS_STAGE = "feedback_weights"
RELATION_EMBEDDINGS_STAGE = "relation_embeddings"
ENTITY_DEDUPLICATION_STAGE = "entity_deduplication"
SUMMARIES_STAGE = "summaries"
GRAPH_RECONCILIATION_STAGE = "graph_reconciliation"
DEFAULT_IMPROVE_STAGES = (
    FEEDBACK_WEIGHTS_STAGE,
    ENTITY_DEDUPLICATION_STAGE,
    RELATION_EMBEDDINGS_STAGE,
)
SUPPORTED_IMPROVE_STAGES = frozenset(
    (*DEFAULT_IMPROVE_STAGES, SUMMARIES_STAGE, GRAPH_RECONCILIATION_STAGE)
)
IMPROVE_RESULT_METRIC_KEY = "improve_result"
FEEDBACK_WEIGHT_ALPHA = 0.1
FEEDBACK_WEIGHT_DECIMALS = 4


class DatasetRepositoryForImprove(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class FeedbackRepositoryForImprove(Protocol):
    async def list_unapplied_for_dataset(self, dataset_id: UUID) -> list[UnappliedFeedback]: ...
    async def mark_applied(self, feedback_id: UUID, *, applied_at: datetime) -> object | None: ...


class EntityMentionRepositoryForImprove(Protocol):
    async def list_for_entities(self, *, entity_ids: list[UUID]) -> list[EntityMention]: ...

    async def list_active_entities_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Entity]: ...


class RelationEvidenceRepositoryForImprove(Protocol):
    async def add(self, evidence: RelationEvidence) -> RelationEvidence: ...

    async def list_for_relations(self, *, relation_ids: list[UUID]) -> list[RelationEvidence]: ...

    async def list_active_relations_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Relation]: ...


class EntityRepositoryForImprove(Protocol):
    async def list_missing_embedding_candidates(
        self,
        *,
        dataset_id: UUID,
    ) -> list[EntityEmbeddingCandidate]: ...

    async def set_missing_embeddings_for_active_current(
        self,
        *,
        dataset_id: UUID,
        embeddings_by_entity_id: dict[UUID, list[float]],
    ) -> int: ...

    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> list[Entity]: ...

    async def list_duplicate_candidates(
        self,
        *,
        dataset_id: UUID,
        similarity_threshold: float,
    ) -> list[EntityDuplicateCandidate]: ...


class RelationRepositoryForImprove(Protocol):
    async def list_missing_embedding_candidates(
        self,
        *,
        dataset_id: UUID,
    ) -> list[RelationEmbeddingCandidate]: ...

    async def set_missing_embeddings_for_active_current(
        self,
        *,
        dataset_id: UUID,
        embeddings_by_relation_id: dict[UUID, list[float]],
    ) -> int: ...

    async def list_active_current_for_dataset(
        self,
        *,
        dataset_id: UUID,
    ) -> list[Relation]: ...


class GraphOutboxRepositoryForImprove(Protocol):
    async def add_projection_command(self, command: ProjectionCommand) -> object: ...


class PipelineRunRepositoryForImprove(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...


class ImproveUnitOfWork(Protocol):
    datasets: DatasetRepositoryForImprove
    feedback: FeedbackRepositoryForImprove
    entities: EntityRepositoryForImprove
    entity_mentions: EntityMentionRepositoryForImprove
    relations: RelationRepositoryForImprove
    relation_evidence: RelationEvidenceRepositoryForImprove
    graph_outbox: GraphOutboxRepositoryForImprove
    pipeline_runs: PipelineRunRepositoryForImprove

    async def __aenter__(self) -> ImproveUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


class GraphProjectionDrain(Protocol):
    async def process_dataset(self, dataset_id: UUID) -> object: ...


class GraphReconciliation(Protocol):
    async def reconcile_dataset(self, dataset_id: UUID) -> GraphReconciliationResult: ...


class GraphMaintenance(Protocol):
    async def maintain_dataset(
        self, dataset_id: UUID, *, generation: int
    ) -> GraphMaintenanceCounts: ...


class SummaryRebuild(Protocol):
    async def rebuild_dataset(
        self, dataset_id: UUID, *, generation: int
    ) -> SummaryRebuildCounts: ...


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


type UnitOfWorkFactory = Callable[[], ImproveUnitOfWork]


@dataclass(frozen=True)
class ImproveDatasetSnapshot:
    id: UUID
    slug: str
    active_generation: int


@dataclass(frozen=True)
class ImproveFeedbackCounts:
    processed: int
    applied: int
    skipped: int
    entities_updated: int
    relations_updated: int
    graph_events_enqueued: int


@dataclass(frozen=True)
class ImproveRelationEmbeddingCounts:
    relations_embedded: int


@dataclass(frozen=True)
class ImproveEntityDeduplicationCounts:
    entities_embedded: int
    duplicate_candidates: int
    entities_merged: int
    entity_mentions_reassigned: int
    relations_rewired: int
    relations_deactivated: int
    relation_evidence_copied: int
    graph_events_enqueued: int


@dataclass(frozen=True)
class ImproveGraphReconciliationCounts:
    entities_missing: int
    entities_extra: int
    chunks_missing: int
    chunks_extra: int
    entity_mentions_missing: int
    entity_mentions_extra: int
    relations_missing: int
    relations_extra: int
    next_missing: int
    next_extra: int
    rebuilt: bool


@dataclass(frozen=True)
class ImproveGraphMaintenanceCounts:
    relations_deactivated: int
    entities_importance_updated: int
    relations_importance_updated: int
    graph_events_enqueued: int


@dataclass(frozen=True)
class RelationMergePlan:
    relation: Relation
    mapped_source_entity_id: UUID
    mapped_target_entity_id: UUID
    changed: bool
    self_loop: bool

    @property
    def identity(self) -> tuple[UUID, UUID, str, int]:
        return (
            self.mapped_source_entity_id,
            self.mapped_target_entity_id,
            self.relation.predicate,
            self.relation.generation,
        )


@dataclass(frozen=True)
class RelationMergeCounts:
    rewired: int
    deactivated: int
    evidence_copied: int


class ImproveService:
    """Apply durable feedback to authoritative PostgreSQL graph weights."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        graph_projection_drain: GraphProjectionDrain,
        graph_reconciliation: GraphReconciliation | None = None,
        graph_maintenance: GraphMaintenance | None = None,
        summary_rebuild: SummaryRebuild | None = None,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._graph_projection_drain = graph_projection_drain
        self._graph_reconciliation = graph_reconciliation
        self._graph_maintenance = graph_maintenance
        self._summary_rebuild = summary_rebuild
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def improve(self, request: ImproveRequest) -> ImproveResult:
        stages = self._supported_stages(request)
        run_id = await self._create_running_run(request, stages)
        try:
            return await self._improve(run_id, request, stages)
        except Exception as exc:
            await self._mark_run_failed(run_id, exc)
            raise

    def _supported_stages(self, request: ImproveRequest) -> list[str]:
        stages = (
            list(DEFAULT_IMPROVE_STAGES)
            if request.stages is None
            else [str(stage) for stage in request.stages]
        )
        unsupported = [stage for stage in stages if stage not in SUPPORTED_IMPROVE_STAGES]
        if unsupported:
            unsupported_json: list[JSONValue] = [stage for stage in unsupported]
            supported_json: list[JSONValue] = [stage for stage in sorted(SUPPORTED_IMPROVE_STAGES)]
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="One or more improve stages are not available.",
                details={"unsupported": unsupported_json, "supported": supported_json},
            )
        return list(dict.fromkeys(stages))

    async def _create_running_run(self, request: ImproveRequest, stages: list[str]) -> UUID:
        run_input = improve_run_input(request, stages)
        now = utc_now()
        run_id = uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.IMPROVE,
            dataset_id=None,
            source_id=None,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=stable_payload_hash(run_input),
            input=run_input,
            progress=0.0,
            current_step=stages[0],
            attempt=1,
            worker_id=None,
            heartbeat_at=None,
            config_fingerprint=self._settings.config_fingerprint(),
            error_code=None,
            error_message=None,
            metrics={},
            started_at=now,
            finished_at=None,
        )
        async with self._unit_of_work_factory() as uow:
            await uow.pipeline_runs.add(run)
            await uow.commit()
        return run_id

    async def _improve(
        self,
        run_id: UUID,
        request: ImproveRequest,
        stages: list[str],
    ) -> ImproveResult:
        dataset = await self._load_dataset_snapshot(request.dataset)
        feedback_counts = ImproveFeedbackCounts(
            processed=0,
            applied=0,
            skipped=0,
            entities_updated=0,
            relations_updated=0,
            graph_events_enqueued=0,
        )
        relation_embedding_counts = ImproveRelationEmbeddingCounts(relations_embedded=0)
        entity_deduplication_counts = ImproveEntityDeduplicationCounts(
            entities_embedded=0,
            duplicate_candidates=0,
            entities_merged=0,
            entity_mentions_reassigned=0,
            relations_rewired=0,
            relations_deactivated=0,
            relation_evidence_copied=0,
            graph_events_enqueued=0,
        )
        graph_reconciliation_counts = _empty_graph_reconciliation_counts()
        graph_maintenance_counts = ImproveGraphMaintenanceCounts(0, 0, 0, 0)
        summary_rebuild_counts = SummaryRebuildCounts(
            document_summaries_rebuilt=0,
            dataset_summaries_rebuilt=0,
            summaries_deactivated=0,
        )
        graph_events_processed = 0

        for stage in stages:
            if stage == FEEDBACK_WEIGHTS_STAGE:
                feedback_counts = await self._apply_feedback_weights(dataset)
                drain_result = await self._graph_projection_drain.process_dataset(dataset.id)
                graph_events_processed += int(getattr(drain_result, "processed", 0))
            elif stage == RELATION_EMBEDDINGS_STAGE:
                relation_embedding_counts = await self._apply_relation_embeddings(dataset)
            elif stage == ENTITY_DEDUPLICATION_STAGE:
                entity_deduplication_counts = await self._detect_entity_duplicates(dataset)
                if entity_deduplication_counts.graph_events_enqueued:
                    drain_result = await self._graph_projection_drain.process_dataset(dataset.id)
                    graph_events_processed += int(getattr(drain_result, "processed", 0))
            elif stage == SUMMARIES_STAGE:
                summary_rebuild_counts = await self._rebuild_summaries(dataset)
            elif stage == GRAPH_RECONCILIATION_STAGE:
                # Reconciliation must observe the Neo4j projection before graph
                # maintenance (hygiene + importance) can enqueue and drain its own
                # entity/relation upserts. Maintenance's entity_upsert commands use
                # Cypher MERGE, which silently recreates a missing Entity node; if
                # maintenance/drain ran first, genuine missing-projection drift
                # would be masked before reconciliation ever reads "actual". Running
                # reconciliation first captures and repairs real drift; maintenance
                # then applies its own authoritative changes afterward exactly as
                # it always did, converging to the same correct final state.
                graph_reconciliation_counts = await self._reconcile_graph(dataset)
                graph_maintenance_counts = await self._maintain_graph(dataset)
                drain_result = await self._graph_projection_drain.process_dataset(dataset.id)
                graph_events_processed += int(getattr(drain_result, "processed", 0))

        result = ImproveResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED.value,
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            stages=stages,
            feedback_processed=feedback_counts.processed,
            feedback_applied=feedback_counts.applied,
            feedback_skipped=feedback_counts.skipped,
            entities_updated=feedback_counts.entities_updated,
            relations_updated=feedback_counts.relations_updated,
            relations_embedded=relation_embedding_counts.relations_embedded,
            entities_embedded=entity_deduplication_counts.entities_embedded,
            entity_duplicate_candidates=entity_deduplication_counts.duplicate_candidates,
            entities_merged=entity_deduplication_counts.entities_merged,
            entity_mentions_reassigned=entity_deduplication_counts.entity_mentions_reassigned,
            relations_rewired=entity_deduplication_counts.relations_rewired,
            relations_deactivated=entity_deduplication_counts.relations_deactivated,
            relation_evidence_copied=entity_deduplication_counts.relation_evidence_copied,
            document_summaries_rebuilt=summary_rebuild_counts.document_summaries_rebuilt,
            dataset_summaries_rebuilt=summary_rebuild_counts.dataset_summaries_rebuilt,
            summaries_deactivated=summary_rebuild_counts.summaries_deactivated,
            graph_relations_deactivated=graph_maintenance_counts.relations_deactivated,
            graph_entities_importance_updated=(
                graph_maintenance_counts.entities_importance_updated
            ),
            graph_relations_importance_updated=(
                graph_maintenance_counts.relations_importance_updated
            ),
            graph_entities_missing=graph_reconciliation_counts.entities_missing,
            graph_entities_extra=graph_reconciliation_counts.entities_extra,
            graph_chunks_missing=graph_reconciliation_counts.chunks_missing,
            graph_chunks_extra=graph_reconciliation_counts.chunks_extra,
            graph_entity_mentions_missing=graph_reconciliation_counts.entity_mentions_missing,
            graph_entity_mentions_extra=graph_reconciliation_counts.entity_mentions_extra,
            graph_relations_missing=graph_reconciliation_counts.relations_missing,
            graph_relations_extra=graph_reconciliation_counts.relations_extra,
            graph_next_missing=graph_reconciliation_counts.next_missing,
            graph_next_extra=graph_reconciliation_counts.next_extra,
            graph_rebuilt=graph_reconciliation_counts.rebuilt,
            graph_events_enqueued=(
                feedback_counts.graph_events_enqueued
                + entity_deduplication_counts.graph_events_enqueued
                + graph_maintenance_counts.graph_events_enqueued
            ),
            graph_events_processed=graph_events_processed,
        )
        await self._mark_run_succeeded(run_id, result, dataset_id=dataset.id)
        return result

    async def _load_dataset_snapshot(self, dataset_slug: str) -> ImproveDatasetSnapshot:
        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_slug(dataset_slug)
            if dataset is None or dataset.status != DatasetStatus.ACTIVE:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Dataset does not exist.",
                    details={"dataset": dataset_slug},
                )
            return ImproveDatasetSnapshot(
                id=dataset.id,
                slug=dataset.slug,
                active_generation=dataset.active_generation,
            )

    async def _apply_feedback_weights(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> ImproveFeedbackCounts:
        async with self._unit_of_work_factory() as uow:
            feedback_items = await uow.feedback.list_unapplied_for_dataset(dataset.id)
            applied = 0
            skipped = 0
            updated_entities: set[UUID] = set()
            updated_relations: set[UUID] = set()
            entity_commands: dict[UUID, ProjectionCommand] = {}
            relation_commands: dict[UUID, ProjectionCommand] = {}

            for feedback in feedback_items:
                chunk_ids = feedback_target_chunk_ids(feedback)
                entities = await uow.entity_mentions.list_active_entities_for_chunks(
                    dataset_id=dataset.id,
                    chunk_ids=chunk_ids,
                )
                relations = await uow.relation_evidence.list_active_relations_for_chunks(
                    dataset_id=dataset.id,
                    chunk_ids=chunk_ids,
                )
                if not entities and not relations:
                    skipped += 1
                    await uow.feedback.mark_applied(feedback.id, applied_at=utc_now())
                    continue

                normalized_score = normalize_feedback_score(feedback.score)
                for entity in entities:
                    next_weight, next_properties = apply_feedback_to_importance(
                        properties=dict(entity.properties),
                        current_importance_weight=float(entity.importance_weight),
                        normalized_score=normalized_score,
                    )
                    entity.properties = next_properties
                    entity.importance_weight = next_weight
                    updated_entities.add(entity.id)
                    entity_commands[entity.id] = entity_upsert_command(
                        entity_id=entity.id,
                        dataset_id=entity.dataset_id,
                        name=entity.name,
                        entity_type=entity.entity_type,
                        description=entity.description,
                        importance_weight=next_weight,
                        generation=entity.generation,
                    )

                for relation in relations:
                    next_weight, next_properties = apply_feedback_to_importance(
                        properties=dict(relation.properties),
                        current_importance_weight=float(relation.importance_weight),
                        normalized_score=normalized_score,
                    )
                    relation.properties = next_properties
                    relation.importance_weight = next_weight
                    updated_relations.add(relation.id)
                    relation_commands[relation.id] = relation_upsert_command(
                        relation_id=relation.id,
                        dataset_id=relation.dataset_id,
                        source_entity_id=relation.source_entity_id,
                        target_entity_id=relation.target_entity_id,
                        predicate=relation.predicate,
                        description=relation.description,
                        confidence=float(relation.confidence),
                        importance_weight=next_weight,
                        generation=relation.generation,
                    )

                applied += 1
                await uow.feedback.mark_applied(feedback.id, applied_at=utc_now())

            graph_events_enqueued = 0
            for command in _ordered_weight_projection_commands(
                entity_commands=entity_commands,
                relation_commands=relation_commands,
            ):
                await uow.graph_outbox.add_projection_command(command)
                graph_events_enqueued += 1

            await uow.commit()
            return ImproveFeedbackCounts(
                processed=len(feedback_items),
                applied=applied,
                skipped=skipped,
                entities_updated=len(updated_entities),
                relations_updated=len(updated_relations),
                graph_events_enqueued=graph_events_enqueued,
            )

    async def _apply_relation_embeddings(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> ImproveRelationEmbeddingCounts:
        async with self._unit_of_work_factory() as uow:
            candidates = await uow.relations.list_missing_embedding_candidates(
                dataset_id=dataset.id,
            )

        if not candidates:
            return ImproveRelationEmbeddingCounts(relations_embedded=0)

        texts = [relation_embedding_text(candidate) for candidate in candidates]
        try:
            embeddings = await self._embedding_client.embed_texts(texts)
        except Exception as exc:
            raise DependencyUnavailableError(
                message="Relation embedding provider is unavailable.",
                cause=exc,
            ) from exc

        validate_embedding_response(
            embeddings,
            expected_count=len(candidates),
            expected_dimensions=self._settings.embedding_dimensions,
            subject="Relation",
        )
        embeddings_by_relation_id = {
            candidate.relation_id: list(embedding)
            for candidate, embedding in zip(candidates, embeddings, strict=True)
        }

        async with self._unit_of_work_factory() as uow:
            relations_embedded = await uow.relations.set_missing_embeddings_for_active_current(
                dataset_id=dataset.id,
                embeddings_by_relation_id=embeddings_by_relation_id,
            )
            await uow.commit()
            return ImproveRelationEmbeddingCounts(relations_embedded=relations_embedded)

    async def _reconcile_graph(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> ImproveGraphReconciliationCounts:
        if self._graph_reconciliation is None:
            raise DependencyUnavailableError(message="Graph reconciliation is unavailable.")
        result = await self._graph_reconciliation.reconcile_dataset(dataset.id)
        return _graph_reconciliation_counts(result)

    async def _maintain_graph(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> ImproveGraphMaintenanceCounts:
        if self._graph_maintenance is None:
            raise DependencyUnavailableError(message="Graph maintenance is unavailable.")
        result = await self._graph_maintenance.maintain_dataset(
            dataset.id,
            generation=dataset.active_generation,
        )
        return ImproveGraphMaintenanceCounts(
            relations_deactivated=result.relations_deactivated,
            entities_importance_updated=result.entities_importance_updated,
            relations_importance_updated=result.relations_importance_updated,
            graph_events_enqueued=result.graph_events_enqueued,
        )

    async def _rebuild_summaries(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> SummaryRebuildCounts:
        if self._summary_rebuild is None:
            raise DependencyUnavailableError(message="Summary rebuild is unavailable.")
        return await self._summary_rebuild.rebuild_dataset(
            dataset.id,
            generation=dataset.active_generation,
        )

    async def _detect_entity_duplicates(
        self,
        dataset: ImproveDatasetSnapshot,
    ) -> ImproveEntityDeduplicationCounts:
        async with self._unit_of_work_factory() as uow:
            candidates = await uow.entities.list_missing_embedding_candidates(dataset_id=dataset.id)

        entities_embedded = 0
        if candidates:
            texts = [entity_embedding_text(candidate) for candidate in candidates]
            try:
                embeddings = await self._embedding_client.embed_texts(texts)
            except Exception as exc:
                raise DependencyUnavailableError(
                    message="Entity embedding provider is unavailable.",
                    cause=exc,
                ) from exc

            validate_embedding_response(
                embeddings,
                expected_count=len(candidates),
                expected_dimensions=self._settings.embedding_dimensions,
                subject="Entity",
            )
            embeddings_by_entity_id = {
                candidate.entity_id: list(embedding)
                for candidate, embedding in zip(candidates, embeddings, strict=True)
            }
            async with self._unit_of_work_factory() as uow:
                entities_embedded = await uow.entities.set_missing_embeddings_for_active_current(
                    dataset_id=dataset.id,
                    embeddings_by_entity_id=embeddings_by_entity_id,
                )
                await uow.commit()

        async with self._unit_of_work_factory() as uow:
            duplicate_candidates = await uow.entities.list_duplicate_candidates(
                dataset_id=dataset.id,
                similarity_threshold=self._settings.entity_dedup_similarity_threshold,
            )
            merge_counts = await self._merge_duplicate_entities(
                uow,
                dataset=dataset,
                candidates=duplicate_candidates,
            )
            if merge_counts.graph_events_enqueued:
                await uow.commit()
        return ImproveEntityDeduplicationCounts(
            entities_embedded=entities_embedded,
            duplicate_candidates=len(duplicate_candidates),
            entities_merged=merge_counts.entities_merged,
            entity_mentions_reassigned=merge_counts.entity_mentions_reassigned,
            relations_rewired=merge_counts.relations_rewired,
            relations_deactivated=merge_counts.relations_deactivated,
            relation_evidence_copied=merge_counts.relation_evidence_copied,
            graph_events_enqueued=merge_counts.graph_events_enqueued,
        )

    async def _merge_duplicate_entities(
        self,
        uow: ImproveUnitOfWork,
        *,
        dataset: ImproveDatasetSnapshot,
        candidates: list[EntityDuplicateCandidate],
    ) -> ImproveEntityDeduplicationCounts:
        merge_candidates = [
            candidate
            for candidate in candidates
            if candidate.similarity >= self._settings.entity_merge_similarity_threshold
        ]
        if not merge_candidates:
            return _empty_entity_merge_counts()

        entity_ids = sorted(
            {
                entity_id
                for candidate in merge_candidates
                for entity_id in (candidate.entity_id, candidate.candidate_id)
            }
        )
        active_entities = await uow.entities.list_active_current_by_ids(
            dataset_id=dataset.id,
            entity_ids=entity_ids,
        )
        entities_by_id = {entity.id: entity for entity in active_entities}
        merge_plan = plan_entity_merges(entities_by_id, merge_candidates)
        if not merge_plan:
            return _empty_entity_merge_counts()

        duplicate_ids = sorted(merge_plan)
        mentions = await uow.entity_mentions.list_for_entities(entity_ids=duplicate_ids)
        relations = await uow.relations.list_active_current_for_dataset(dataset_id=dataset.id)
        relation_evidence = await uow.relation_evidence.list_for_relations(
            relation_ids=sorted(relation.id for relation in relations)
        )

        graph_commands: list[ProjectionCommand] = []
        for duplicate_id in duplicate_ids:
            survivor = entities_by_id[merge_plan[duplicate_id]]
            duplicate = entities_by_id[duplicate_id]
            survivor.aliases = merged_entity_aliases(survivor=survivor, duplicate=duplicate)
            duplicate.is_active = False
            graph_commands.append(
                entity_delete_command(entity_id=duplicate.id, dataset_id=dataset.id)
            )

        mention_commands = reassign_entity_mentions(
            mentions=mentions,
            entity_id_mapping=merge_plan,
            dataset_id=dataset.id,
        )
        graph_commands.extend(mention_commands)

        relation_counts, relation_commands = await merge_relations(
            uow,
            relations=relations,
            relation_evidence=relation_evidence,
            entity_id_mapping=merge_plan,
            dataset_id=dataset.id,
        )
        graph_commands.extend(relation_commands)

        for command in graph_commands:
            await uow.graph_outbox.add_projection_command(command)

        return ImproveEntityDeduplicationCounts(
            entities_embedded=0,
            duplicate_candidates=0,
            entities_merged=len(duplicate_ids),
            entity_mentions_reassigned=len(mention_commands),
            relations_rewired=relation_counts.rewired,
            relations_deactivated=relation_counts.deactivated,
            relation_evidence_copied=relation_counts.evidence_copied,
            graph_events_enqueued=len(graph_commands),
        )

    async def _mark_run_succeeded(
        self,
        run_id: UUID,
        result: ImproveResult,
        *,
        dataset_id: UUID,
    ) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            run.dataset_id = dataset_id
            run.status = PipelineRunStatus.SUCCEEDED
            run.progress = 1.0
            run.current_step = None
            run.error_code = None
            run.error_message = None
            run.metrics = {IMPROVE_RESULT_METRIC_KEY: result.model_dump(mode="json")}
            run.finished_at = utc_now()
            await uow.commit()

    async def _mark_run_failed(self, run_id: UUID, exc: Exception) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            run.status = PipelineRunStatus.FAILED
            run.progress = 1.0
            run.current_step = None
            run.error_code = type(exc).__name__
            run.error_message = "Improve failed."
            run.finished_at = utc_now()
            await uow.commit()


def normalize_feedback_score(score: int) -> float:
    if score == -1:
        return 0.0
    if score == 0:
        return 0.5
    if score == 1:
        return 1.0
    raise ValueError("feedback score must be -1, 0, or 1")


def stream_update_weight(
    previous_weight: float,
    normalized_feedback: float,
    *,
    alpha: float = FEEDBACK_WEIGHT_ALPHA,
) -> float:
    if alpha <= 0 or alpha > 1:
        raise ValueError("alpha must be greater than 0 and less than or equal to 1")
    updated = previous_weight + alpha * (normalized_feedback - previous_weight)
    clamped = max(0.0, min(1.0, float(updated)))
    return round(clamped, FEEDBACK_WEIGHT_DECIMALS)


def apply_feedback_to_importance(
    *,
    properties: dict[str, object],
    current_importance_weight: float,
    normalized_score: float,
) -> tuple[float, dict[str, object]]:
    if not has_importance_marker(properties):
        return stream_update_weight(current_importance_weight, normalized_score), properties

    components = importance_components_from_properties(
        properties,
        fallback_feedback_weight=current_importance_weight,
    )
    next_feedback_weight = stream_update_weight(components.feedback_weight, normalized_score)
    next_properties = properties_with_importance_marker(
        properties,
        ImportanceComponents(
            feedback_weight=next_feedback_weight,
            centrality_weight=components.centrality_weight,
        ),
    )
    next_components = importance_components_from_properties(
        next_properties,
        fallback_feedback_weight=current_importance_weight,
    )
    return next_components.effective_weight, next_properties


def relation_embedding_text(candidate: RelationEmbeddingCandidate) -> str:
    source_name = candidate.source_name.strip()
    target_name = candidate.target_name.strip()
    predicate = candidate.predicate.strip()
    description = candidate.description.strip()
    relationship_text = predicate if not description else f"{predicate}: {description}"
    return f"{source_name}-›{relationship_text}-›{target_name}"


def entity_embedding_text(candidate: EntityEmbeddingCandidate) -> str:
    return candidate.name.strip()


def validate_embedding_response(
    embeddings: Sequence[Sequence[float]],
    *,
    expected_count: int,
    expected_dimensions: int,
    subject: str,
) -> None:
    if len(embeddings) != expected_count:
        raise DependencyUnavailableError(
            message=f"{subject} embedding provider returned an invalid response.",
            details={"reason": "count_mismatch"},
        )
    for embedding in embeddings:
        if len(embedding) != expected_dimensions:
            raise DependencyUnavailableError(
                message=f"{subject} embedding provider returned an invalid response.",
                details={"reason": "dimension_mismatch"},
            )


def plan_entity_merges(
    entities_by_id: dict[UUID, Entity],
    candidates: Sequence[EntityDuplicateCandidate],
) -> dict[UUID, UUID]:
    adjacency: dict[UUID, set[UUID]] = {}
    for candidate in candidates:
        if (
            candidate.entity_id not in entities_by_id
            or candidate.candidate_id not in entities_by_id
        ):
            continue
        adjacency.setdefault(candidate.entity_id, set()).add(candidate.candidate_id)
        adjacency.setdefault(candidate.candidate_id, set()).add(candidate.entity_id)

    consumed: set[UUID] = set()
    entity_id_mapping: dict[UUID, UUID] = {}
    for survivor in sorted(entities_by_id.values(), key=_entity_survivor_sort_key):
        if survivor.id in consumed:
            continue
        direct_neighbors = [
            entity_id
            for entity_id in adjacency.get(survivor.id, set())
            if entity_id not in consumed and entity_id != survivor.id
        ]
        for duplicate_id in sorted(
            direct_neighbors,
            key=lambda entity_id: _entity_survivor_sort_key(entities_by_id[entity_id]),
        ):
            if duplicate_id in consumed:
                continue
            entity_id_mapping[duplicate_id] = survivor.id
            consumed.add(duplicate_id)
    return entity_id_mapping


def merged_entity_aliases(*, survivor: Entity, duplicate: Entity) -> list[str]:
    survivor_name_key = survivor.name.strip().casefold()
    aliases: list[str] = []
    seen: set[str] = set()
    for raw_alias in [*survivor.aliases, duplicate.name, *duplicate.aliases]:
        alias = raw_alias.strip()
        alias_key = alias.casefold()
        if not alias or alias_key == survivor_name_key or alias_key in seen:
            continue
        aliases.append(alias)
        seen.add(alias_key)
    return aliases


def reassign_entity_mentions(
    *,
    mentions: Sequence[EntityMention],
    entity_id_mapping: dict[UUID, UUID],
    dataset_id: UUID,
) -> list[ProjectionCommand]:
    commands: list[ProjectionCommand] = []
    for mention in sorted(mentions, key=lambda item: item.id):
        survivor_id = entity_id_mapping.get(mention.entity_id)
        if survivor_id is None:
            continue
        mention.entity_id = survivor_id
        commands.append(
            entity_mention_upsert_command(
                mention_id=mention.id,
                dataset_id=dataset_id,
                entity_id=survivor_id,
                chunk_id=mention.chunk_id,
                confidence=float(mention.confidence),
            )
        )
    return commands


async def merge_relations(
    uow: ImproveUnitOfWork,
    *,
    relations: Sequence[Relation],
    relation_evidence: Sequence[RelationEvidence],
    entity_id_mapping: dict[UUID, UUID],
    dataset_id: UUID,
) -> tuple[RelationMergeCounts, list[ProjectionCommand]]:
    plans = [
        _relation_merge_plan(relation, entity_id_mapping)
        for relation in sorted(relations, key=_relation_sort_key)
    ]
    evidence_by_relation = _relation_evidence_by_relation(relation_evidence)

    rewired = 0
    deactivated = 0
    evidence_copied = 0
    commands_by_relation_id: dict[UUID, ProjectionCommand] = {}
    grouped_plans: dict[tuple[UUID, UUID, str, int], list[RelationMergePlan]] = {}

    for plan in plans:
        if not plan.changed:
            grouped_plans.setdefault(plan.identity, []).append(plan)
            continue
        if plan.self_loop:
            plan.relation.is_active = False
            plan.relation.embedding = None
            deactivated += 1
            continue
        grouped_plans.setdefault(plan.identity, []).append(plan)

    for group in grouped_plans.values():
        if not any(plan.changed for plan in group):
            continue
        survivor_plan = min(group, key=_relation_survivor_plan_sort_key)
        loser_plans = [plan for plan in group if plan.relation.id != survivor_plan.relation.id]
        survivor = survivor_plan.relation

        if survivor_plan.changed:
            survivor.source_entity_id = survivor_plan.mapped_source_entity_id
            survivor.target_entity_id = survivor_plan.mapped_target_entity_id
            survivor.embedding = None
            rewired += 1

        if loser_plans:
            survivor.confidence = max(float(plan.relation.confidence) for plan in group)
            survivor.importance_weight = max(
                float(plan.relation.importance_weight) for plan in group
            )

        for loser_plan in loser_plans:
            loser = loser_plan.relation
            if loser.is_active:
                loser.is_active = False
                loser.embedding = None
                deactivated += 1
            evidence_copied += await _copy_missing_relation_evidence(
                uow,
                survivor=survivor,
                loser=loser,
                evidence_by_relation=evidence_by_relation,
            )

        if survivor_plan.changed or loser_plans:
            commands_by_relation_id[survivor.id] = relation_upsert_command(
                relation_id=survivor.id,
                dataset_id=dataset_id,
                source_entity_id=survivor.source_entity_id,
                target_entity_id=survivor.target_entity_id,
                predicate=survivor.predicate,
                description=survivor.description,
                confidence=float(survivor.confidence),
                importance_weight=float(survivor.importance_weight),
                generation=survivor.generation,
            )

    return (
        RelationMergeCounts(
            rewired=rewired,
            deactivated=deactivated,
            evidence_copied=evidence_copied,
        ),
        [commands_by_relation_id[relation_id] for relation_id in sorted(commands_by_relation_id)],
    )


def feedback_target_chunk_ids(feedback: UnappliedFeedback) -> list[UUID]:
    if feedback.target_type == REFERENCE_TARGET_TYPE:
        return [feedback.target_id] if feedback.target_id is not None else []
    if feedback.target_type == ANSWER_TARGET_TYPE:
        return sorted(reference_chunk_ids(feedback.references))
    return []


def improve_run_input(request: ImproveRequest, stages: list[str]) -> dict[str, JSONValue]:
    stages_json: list[JSONValue] = list(stages)
    return {
        "dataset": request.dataset,
        "stages": stages_json,
        "wait": request.wait,
    }


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> ImproveUnitOfWork:
        return cast(ImproveUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work


def _ordered_weight_projection_commands(
    *,
    entity_commands: dict[UUID, ProjectionCommand],
    relation_commands: dict[UUID, ProjectionCommand],
) -> tuple[ProjectionCommand, ...]:
    return (
        *[entity_commands[entity_id] for entity_id in sorted(entity_commands)],
        *[relation_commands[relation_id] for relation_id in sorted(relation_commands)],
    )


def _empty_entity_merge_counts() -> ImproveEntityDeduplicationCounts:
    return ImproveEntityDeduplicationCounts(
        entities_embedded=0,
        duplicate_candidates=0,
        entities_merged=0,
        entity_mentions_reassigned=0,
        relations_rewired=0,
        relations_deactivated=0,
        relation_evidence_copied=0,
        graph_events_enqueued=0,
    )


def _empty_graph_reconciliation_counts() -> ImproveGraphReconciliationCounts:
    return _graph_reconciliation_counts(
        GraphReconciliationResult(diff=GraphReconciliationDiff(), rebuilt=False)
    )


def _graph_reconciliation_counts(
    result: GraphReconciliationResult,
) -> ImproveGraphReconciliationCounts:
    diff = result.diff
    return ImproveGraphReconciliationCounts(
        entities_missing=diff.entities_missing,
        entities_extra=diff.entities_extra,
        chunks_missing=diff.chunks_missing,
        chunks_extra=diff.chunks_extra,
        entity_mentions_missing=diff.entity_mentions_missing,
        entity_mentions_extra=diff.entity_mentions_extra,
        relations_missing=diff.relations_missing,
        relations_extra=diff.relations_extra,
        next_missing=diff.next_missing,
        next_extra=diff.next_extra,
        rebuilt=result.rebuilt,
    )


def _entity_survivor_sort_key(entity: Entity) -> tuple[datetime, UUID]:
    return (_created_at(entity.created_at), entity.id)


def _relation_sort_key(relation: Relation) -> tuple[datetime, UUID]:
    return (_created_at(relation.created_at), relation.id)


def _created_at(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _relation_merge_plan(
    relation: Relation,
    entity_id_mapping: dict[UUID, UUID],
) -> RelationMergePlan:
    mapped_source = entity_id_mapping.get(relation.source_entity_id, relation.source_entity_id)
    mapped_target = entity_id_mapping.get(relation.target_entity_id, relation.target_entity_id)
    changed = (
        mapped_source != relation.source_entity_id or mapped_target != relation.target_entity_id
    )
    return RelationMergePlan(
        relation=relation,
        mapped_source_entity_id=mapped_source,
        mapped_target_entity_id=mapped_target,
        changed=changed,
        self_loop=mapped_source == mapped_target,
    )


def _relation_survivor_plan_sort_key(plan: RelationMergePlan) -> tuple[int, datetime, UUID]:
    canonical_endpoint_priority = 1 if plan.changed else 0
    return (canonical_endpoint_priority, *_relation_sort_key(plan.relation))


def _relation_evidence_by_relation(
    evidence_items: Sequence[RelationEvidence],
) -> dict[UUID, list[RelationEvidence]]:
    result: dict[UUID, list[RelationEvidence]] = {}
    for evidence in evidence_items:
        result.setdefault(evidence.relation_id, []).append(evidence)
    return result


async def _copy_missing_relation_evidence(
    uow: ImproveUnitOfWork,
    *,
    survivor: Relation,
    loser: Relation,
    evidence_by_relation: dict[UUID, list[RelationEvidence]],
) -> int:
    survivor_evidence = evidence_by_relation.setdefault(survivor.id, [])
    existing_chunk_ids = {evidence.chunk_id for evidence in survivor_evidence}
    copied = 0
    for evidence in evidence_by_relation.get(loser.id, []):
        if evidence.chunk_id in existing_chunk_ids:
            continue
        copied_evidence = RelationEvidence(
            relation_id=survivor.id,
            chunk_id=evidence.chunk_id,
            quote=evidence.quote,
            confidence=evidence.confidence,
        )
        await uow.relation_evidence.add(copied_evidence)
        survivor_evidence.append(copied_evidence)
        existing_chunk_ids.add(evidence.chunk_id)
        copied += 1
    return copied
