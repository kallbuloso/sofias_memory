"""Explicit Improve v1 service for applying persisted feedback weights."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, PipelineRun, Relation
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import ProjectionCommand, entity_upsert_command, relation_upsert_command
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.improve import ImproveRequest, ImproveResult
from sofias_memory.services.feedback import (
    ANSWER_TARGET_TYPE,
    REFERENCE_TARGET_TYPE,
    reference_chunk_ids,
)
from sofias_memory.services.remember import stable_payload_hash

FEEDBACK_WEIGHTS_STAGE = "feedback_weights"
SUPPORTED_IMPROVE_STAGES = frozenset((FEEDBACK_WEIGHTS_STAGE,))
IMPROVE_RESULT_METRIC_KEY = "improve_result"
FEEDBACK_WEIGHT_ALPHA = 0.1
FEEDBACK_WEIGHT_DECIMALS = 4


class DatasetRepositoryForImprove(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class FeedbackRepositoryForImprove(Protocol):
    async def list_unapplied_for_dataset(self, dataset_id: UUID) -> list[UnappliedFeedback]: ...
    async def mark_applied(self, feedback_id: UUID, *, applied_at: datetime) -> object | None: ...


class EntityMentionRepositoryForImprove(Protocol):
    async def list_active_entities_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Entity]: ...


class RelationEvidenceRepositoryForImprove(Protocol):
    async def list_active_relations_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Relation]: ...


class GraphOutboxRepositoryForImprove(Protocol):
    async def add_projection_command(self, command: ProjectionCommand) -> object: ...


class PipelineRunRepositoryForImprove(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...


class ImproveUnitOfWork(Protocol):
    datasets: DatasetRepositoryForImprove
    feedback: FeedbackRepositoryForImprove
    entity_mentions: EntityMentionRepositoryForImprove
    relation_evidence: RelationEvidenceRepositoryForImprove
    graph_outbox: GraphOutboxRepositoryForImprove
    pipeline_runs: PipelineRunRepositoryForImprove

    async def __aenter__(self) -> ImproveUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


class GraphProjectionDrain(Protocol):
    async def process_dataset(self, dataset_id: UUID) -> object: ...


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


class ImproveService:
    """Apply durable feedback to authoritative PostgreSQL graph weights."""

    def __init__(
        self,
        settings: Settings,
        *,
        graph_projection_drain: GraphProjectionDrain,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._graph_projection_drain = graph_projection_drain
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
            [FEEDBACK_WEIGHTS_STAGE]
            if request.stages is None
            else [str(stage) for stage in request.stages]
        )
        unsupported = [stage for stage in stages if stage not in SUPPORTED_IMPROVE_STAGES]
        if unsupported:
            unsupported_json: list[JSONValue] = list(unsupported)
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Only feedback_weights is available in this checkpoint.",
                details={"stages": unsupported_json},
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
            current_step=FEEDBACK_WEIGHTS_STAGE,
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
        counts = await self._apply_feedback_weights(dataset)
        drain_result = await self._graph_projection_drain.process_dataset(dataset.id)
        graph_events_processed = int(getattr(drain_result, "processed", 0))
        result = ImproveResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED.value,
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            stages=stages,
            feedback_processed=counts.processed,
            feedback_applied=counts.applied,
            feedback_skipped=counts.skipped,
            entities_updated=counts.entities_updated,
            relations_updated=counts.relations_updated,
            graph_events_enqueued=counts.graph_events_enqueued,
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
                    previous_weight = float(entity.importance_weight)
                    next_weight = stream_update_weight(previous_weight, normalized_score)
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
                    previous_weight = float(relation.importance_weight)
                    next_weight = stream_update_weight(previous_weight, normalized_score)
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
