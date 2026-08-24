"""Improve pipeline steps (SM-511, ADR-0009 SS O).

**Fixed execution slots, not a per-request pipeline (SM-511 MAJOR 1).** The
registered ``PipelineDefinition`` is completely static -- five *slots*
(``SLOT_COUNT``), each with three fixed sub-phases (``pre``, ``main``,
``post``), always present and always executed in the same order:

    slot_0: pre, main, post
    slot_1: pre, main, post
    slot_2: pre, main, post
    slot_3: pre, main, post
    slot_4: pre, main, post
    final_convergence
    finalize_result

A slot's *runtime identity* -- which of the five approved public stages (if
any) occupies it -- is derived purely from ``run_input["stages"]`` (already
normalized: defaulted, deduplicated preserving first occurrence, but with
request order otherwise preserved verbatim -- ``services.improve.
normalize_improve_stages``). Slot ``i`` is stage ``stages[i]`` if that index
exists, else empty. This is how request order survives a closed, code-only
registry: no ``PipelineDefinition`` is ever built per-request, no dynamic
import, no client-defined step -- only a fixed dispatch table (``SlotStep``,
``PHASE_HANDLERS``) that already exists in code for exactly the five
approved stages. A slot beyond the number of requested stages, or a phase a
stage doesn't need, is a deterministic no-op (``SlotStep._handler`` returns
``None``), still observable per-step in ``PipelineStep.output``.

Per-stage phase usage (why ``pre``/``main``/``post`` exist):

- ``feedback_weights``: ``main`` only -- zero external dependency, so its
  entire read+compute+mutate+enqueue sequence lives in one ``persist``.
- ``entity_deduplication``: ``pre`` (embed missing entities, staged batch)
  then ``main`` (duplicate detection + merge, PostgreSQL-only) -- two
  *sequential, committed* phases because the merge query must see this same
  run's own freshly-committed embeddings.
- ``relation_embeddings``: ``main`` only -- a single step already contains
  both the external embed call (``execute``) and the persist half.
- ``summaries``: ``main`` only -- same staged-batch shape (LLM+embeddings
  in ``execute``, PostgreSQL-only ``persist``).
- ``graph_reconciliation``: ``pre`` (drain this run's own prior graph_outbox
  writes, then Neo4j reconcile) -> ``main`` (PostgreSQL-only maintenance) ->
  ``post`` (drain what maintenance enqueued). Reproduces B4's frozen
  invariant (reconcile -> maintain -> drain) *inside* its slot, regardless of
  where that slot sits relative to the other requested stages.

**Final projection convergence (SM-511 MAJOR 2).** Every selected stage that
can enqueue ``graph_outbox`` rows (feedback_weights, entity_deduplication,
graph_maintain) may leave work for the autonomous SM-506 consumer to pick
up -- concurrently, benignly, via the same claim-or-observe leasing. That
consumer racing ahead is fine; what is not acceptable is this run reaching
``SUCCEEDED`` while it has NOT itself observed convergence. ``final_
convergence`` (always present, unconditional) runs after every slot has
committed and drains whatever remains outstanding for the dataset,
regardless of which stage(s) were selected or whether ``graph_reconciliation``
was requested at all. Because ``GraphOutboxBatchProcessor.process_dataset``
snapshots ``graph_outbox`` and blocks on each row until it reaches a
terminal (Postgres-authoritative) state -- whether this call or the
autonomous consumer performs the underlying projection write -- a return
from this step is proof of convergence, not a race-prone local counter.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineType
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError, RetryablePipelineStepError
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    PipelineDefinition,
    PipelineStep,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
    no_op_persist,
)
from sofias_memory.ports import (
    ProjectionCommand,
    entity_delete_command,
    entity_upsert_command,
    relation_upsert_command,
)
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.graph_maintenance_service import (
    GraphMaintenanceService,
    apply_maintenance_plan,
    compute_maintenance_plan_with_uow,
)
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from sofias_memory.services.improve import (
    ENTITY_DEDUPLICATION_STAGE,
    FEEDBACK_WEIGHTS_STAGE,
    GRAPH_RECONCILIATION_STAGE,
    IMPROVE_RESULT_METRIC_KEY,
    RELATION_EMBEDDINGS_STAGE,
    SUMMARIES_STAGE,
    apply_feedback_to_importance,
    apply_relation_merge_plan,
    entity_embedding_text,
    feedback_target_chunk_ids,
    merged_entity_aliases,
    normalize_feedback_score,
    plan_entity_merges,
    plan_relation_merges,
    reassign_entity_mentions,
    relation_embedding_text,
    validate_embedding_response,
)
from sofias_memory.services.summary_rebuild_service import (
    PreparedSummaryRebuild,
    SummaryRebuildService,
    apply_prepared_summaries,
)

STAGED_BATCH_MAX_AGE_SECONDS = 900.0
"""Same hygiene bound as Cognify's (``pipelines.steps.cognify``): only a
fenced/superseded attempt's staged entry can outlive its owning short
transaction, and this sweep drops it. Never a correctness mechanism -- every
staged batch here is transient, recomputable state."""

IMPROVE_RESOURCES_RESOURCE = "improve_resources"
"""``PipelineContext.resources`` key holding :class:`ImprovePipelineResources`
-- built once at application startup, never per request."""

IMPROVE_SCOPE_ERROR_CODE = "IMPROVE_RUN_SCOPE_INVALID"
IMPROVE_SCOPE_ERROR_MESSAGE = "Improve run is not scoped to a dataset."
IMPROVE_RESOURCE_MISSING_ERROR_CODE = "IMPROVE_RESOURCE_MISSING"
IMPROVE_RESOURCE_MISSING_MESSAGE = "Improve processing resources are not configured."
IMPROVE_BATCH_MISSING_ERROR_CODE = "IMPROVE_PREPARED_BATCH_MISSING"
IMPROVE_BATCH_MISSING_MESSAGE = "Improve prepared batch is not available for this run."
IMPROVE_DEPENDENCY_ERROR_CODE = "IMPROVE_DEPENDENCY_UNAVAILABLE"
IMPROVE_DEPENDENCY_ERROR_MESSAGE = "An Improve dependency was unavailable."
IMPROVE_ACTIVATION_ERROR_CODE = "IMPROVE_FINALIZE_TARGET_MISSING"
IMPROVE_ACTIVATION_ERROR_MESSAGE = "Improve finalize target no longer exists."

SLOT_COUNT = 5
PRE_PHASE = "pre"
MAIN_PHASE = "main"
POST_PHASE = "post"

FINAL_CONVERGENCE_STEP = "final_convergence"
FINALIZE_RESULT_STEP = "finalize_result"

_DEFINITION_ID_PREFIX = "improve."


def resolve_slot_stages(run_input: Mapping[str, Any]) -> list[str | None]:
    """Pure, deterministic mapping ``run_input -> per-slot stage identity``.

    Callable identically at submission time and at every execution/recovery
    checkpoint (ADR-0009 SS 11/SS 12): the same ``run_input`` always yields
    the same slots. ``run_input["stages"]`` is already the normalized,
    order-preserving list (``services.improve.normalize_improve_stages``);
    this only pads/truncates it to :data:`SLOT_COUNT`.
    """

    stages = run_input.get("stages")
    ordered = [str(stage) for stage in stages] if isinstance(stages, list) else []
    padded: list[str | None] = list(ordered[:SLOT_COUNT])
    padded.extend([None] * (SLOT_COUNT - len(padded)))
    return padded


def slot_step_name(slot_index: int, phase: str) -> str:
    return f"stage_slot_{slot_index}_{phase}"


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ImprovePipelineResources:
    """Everything an Improve step needs, built once per process (SM-511 SS
    12): one shared embedding client (also reused by ``summary_rebuild``,
    matching B4's wiring), and the three graph services B4 already used."""

    settings: Settings
    embedding_client: EmbeddingClient
    graph_maintenance: GraphMaintenanceService
    summary_rebuild: SummaryRebuildService
    # Absent (``None``) only when the process has no Neo4j resource at all
    # (e.g. ``enable_neo4j=False`` in a test app) -- feedback_weights,
    # entity_deduplication, relation_embeddings and summaries never touch
    # these, so building the other four resources must not be blocked on
    # Neo4j availability. A slot that actually resolves to
    # ``graph_reconciliation`` surfaces absence as a typed permanent error
    # instead of an ``AttributeError``; ``final_convergence`` degrades to a
    # documented no-op instead (see its own docstring).
    graph_reconciliation: GraphReconciliationService | None
    graph_outbox_drain: GraphOutboxBatchProcessor | None


def _resources(context: PipelineContext) -> ImprovePipelineResources:
    resource = context.resources.get(IMPROVE_RESOURCES_RESOURCE)
    if resource is None:
        raise PermanentPipelineStepError(
            IMPROVE_RESOURCE_MISSING_ERROR_CODE, IMPROVE_RESOURCE_MISSING_MESSAGE
        )
    return cast(ImprovePipelineResources, resource)


def _dataset_id(context: PipelineContext) -> UUID:
    if context.dataset_id is None:
        raise PermanentPipelineStepError(IMPROVE_SCOPE_ERROR_CODE, IMPROVE_SCOPE_ERROR_MESSAGE)
    return context.dataset_id


class _StagedBatchCache[T]:
    """Private, process-local, per-step-instance cache handing a computed
    batch from ``execute`` to the ``persist`` the engine calls in the very
    next statement (Cognify's established pattern, ``pipelines.steps.
    cognify``). Never a source of truth -- losing an entry costs, at most,
    redoing ``execute``'s external work on the next attempt."""

    def __init__(self) -> None:
        self._staged: dict[UUID, tuple[float, T]] = {}

    def stage(self, run_id: UUID, batch: T) -> None:
        self._evict_abandoned()
        self._staged[run_id] = (time.monotonic(), batch)

    def pop(self, run_id: UUID) -> T | None:
        staged = self._staged.pop(run_id, None)
        return staged[1] if staged is not None else None

    def drop(self, run_id: UUID) -> None:
        self._staged.pop(run_id, None)

    def _evict_abandoned(self) -> None:
        cutoff = time.monotonic() - STAGED_BATCH_MAX_AGE_SECONDS
        for run_id in [rid for rid, (staged_at, _) in self._staged.items() if staged_at < cutoff]:
            del self._staged[run_id]


# ---------------------------------------------------------------------------
# feedback_weights -- no external dependency, entirely in persist().
# ---------------------------------------------------------------------------


class FeedbackWeightsStep:
    """ATOMIC: zero external dependency, so the whole read+compute+mutate
    sequence lives in ``persist`` (engine-owned transaction) -- nothing
    commits anywhere else, so an orphaned RUNNING row proves nothing was
    committed. Invoked only when the owning slot resolves to
    ``feedback_weights`` (``SlotStep`` decides that, not this class)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        feedback_items = await uow.feedback.list_unapplied_for_dataset(dataset_id)
        applied = 0
        skipped = 0
        updated_entities: set[UUID] = set()
        updated_relations: set[UUID] = set()
        entity_commands: dict[UUID, ProjectionCommand] = {}
        relation_commands: dict[UUID, ProjectionCommand] = {}
        now = utc_now()

        for feedback in feedback_items:
            chunk_ids = feedback_target_chunk_ids(feedback)
            entities = await uow.entity_mentions.list_active_entities_for_chunks(
                dataset_id=dataset_id, chunk_ids=chunk_ids
            )
            relations = await uow.relation_evidence.list_active_relations_for_chunks(
                dataset_id=dataset_id, chunk_ids=chunk_ids
            )
            if not entities and not relations:
                skipped += 1
                await uow.feedback.mark_applied(feedback.id, applied_at=now)
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
            await uow.feedback.mark_applied(feedback.id, applied_at=now)

        graph_events_enqueued = 0
        for entity_id in sorted(entity_commands):
            await uow.graph_outbox.add_projection_command(entity_commands[entity_id])
            graph_events_enqueued += 1
        for relation_id in sorted(relation_commands):
            await uow.graph_outbox.add_projection_command(relation_commands[relation_id])
            graph_events_enqueued += 1

        result.output.update(
            {
                "processed": len(feedback_items),
                "applied": applied,
                "skipped": skipped,
                "entities_updated": len(updated_entities),
                "relations_updated": len(updated_relations),
                "graph_events_enqueued": graph_events_enqueued,
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# entity_embeddings (entity_deduplication, pre) -- staged-batch split.
# ---------------------------------------------------------------------------


class EntityEmbeddingsStep:
    """ATOMIC: ``execute`` never writes anything; ``persist`` applies the
    whole staged batch in the engine's own transaction."""

    def __init__(self) -> None:
        self._staged: _StagedBatchCache[dict[UUID, list[float]]] = _StagedBatchCache()

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)

        async with PostgresUnitOfWork(context.session_factory) as uow:
            candidates = await uow.entities.list_missing_embedding_candidates(dataset_id=dataset_id)

        if not candidates:
            self._staged.stage(context.run_id, {})
            return StepResult(output={"entities_embedded": 0})

        texts = [entity_embedding_text(candidate) for candidate in candidates]
        embeddings = await _embed(resources, texts, subject="Entity")
        validate_embedding_response(
            embeddings,
            expected_count=len(candidates),
            expected_dimensions=resources.settings.embedding_dimensions,
            subject="Entity",
        )
        embeddings_by_id = {
            candidate.entity_id: list(embedding)
            for candidate, embedding in zip(candidates, embeddings, strict=True)
        }
        self._staged.stage(context.run_id, embeddings_by_id)
        return StepResult(output={"entities_embedded": len(embeddings_by_id)})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        embeddings_by_id = self._staged.pop(context.run_id)
        if embeddings_by_id is None:
            raise PermanentPipelineStepError(
                IMPROVE_BATCH_MISSING_ERROR_CODE, IMPROVE_BATCH_MISSING_MESSAGE
            )
        if not embeddings_by_id:
            return
        actual = await uow.entities.set_missing_embeddings_for_active_current(
            dataset_id=_dataset_id(context),
            embeddings_by_entity_id=embeddings_by_id,
        )
        result.output["entities_embedded"] = actual

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id)
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# entity_merge (entity_deduplication, main) -- entirely in persist().
# ---------------------------------------------------------------------------


class EntityMergeStep:
    """ATOMIC: no external dependency (duplicate detection and merge
    planning are pure PostgreSQL reads/computation over already-embedded
    entities -- committed by this slot's own ``pre`` phase before this phase
    runs), so this step's whole sequence lives in ``persist``."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        resources = _resources(context)

        duplicate_candidates = await uow.entities.list_duplicate_candidates(
            dataset_id=dataset_id,
            similarity_threshold=resources.settings.entity_dedup_similarity_threshold,
        )
        result.output["duplicate_candidates"] = len(duplicate_candidates)
        merge_candidates = [
            candidate
            for candidate in duplicate_candidates
            if candidate.similarity >= resources.settings.entity_merge_similarity_threshold
        ]
        if not merge_candidates:
            return

        entity_ids = sorted(
            {
                eid
                for candidate in merge_candidates
                for eid in (candidate.entity_id, candidate.candidate_id)
            }
        )
        active_entities = await uow.entities.list_active_current_by_ids(
            dataset_id=dataset_id, entity_ids=entity_ids
        )
        entities_by_id = {entity.id: entity for entity in active_entities}
        merge_plan = plan_entity_merges(entities_by_id, merge_candidates)
        if not merge_plan:
            return

        duplicate_ids = sorted(merge_plan)
        graph_commands: list[ProjectionCommand] = []
        for duplicate_id in duplicate_ids:
            survivor = entities_by_id[merge_plan[duplicate_id]]
            duplicate = entities_by_id[duplicate_id]
            survivor.aliases = merged_entity_aliases(survivor=survivor, duplicate=duplicate)
            duplicate.is_active = False
            graph_commands.append(
                entity_delete_command(entity_id=duplicate.id, dataset_id=dataset_id)
            )

        mentions = await uow.entity_mentions.list_for_entities(entity_ids=duplicate_ids)
        mention_commands = reassign_entity_mentions(
            mentions=mentions, entity_id_mapping=merge_plan, dataset_id=dataset_id
        )
        graph_commands.extend(mention_commands)

        relations = await uow.relations.list_active_current_for_dataset(dataset_id=dataset_id)
        relation_evidence = await uow.relation_evidence.list_for_relations(
            relation_ids=sorted(relation.id for relation in relations)
        )
        relation_plan = plan_relation_merges(
            relations=relations,
            relation_evidence=relation_evidence,
            entity_id_mapping=merge_plan,
        )
        relation_counts, relation_commands = await apply_relation_merge_plan(
            relations_repo=uow.relations,
            relation_evidence_repo=uow.relation_evidence,
            dataset_id=dataset_id,
            plan=relation_plan,
        )
        graph_commands.extend(relation_commands)

        for command in graph_commands:
            await uow.graph_outbox.add_projection_command(command)

        result.output.update(
            {
                "entities_merged": len(duplicate_ids),
                "entity_mentions_reassigned": len(mention_commands),
                "relations_rewired": relation_counts.rewired,
                "relations_deactivated": relation_counts.deactivated,
                "relation_evidence_copied": relation_counts.evidence_copied,
                "graph_events_enqueued": len(graph_commands),
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# relation_embeddings -- staged-batch split (single step, both halves).
# ---------------------------------------------------------------------------


class RelationEmbeddingsStep:
    """ATOMIC: same shape as :class:`EntityEmbeddingsStep`."""

    def __init__(self) -> None:
        self._staged: _StagedBatchCache[dict[UUID, list[float]]] = _StagedBatchCache()

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)

        async with PostgresUnitOfWork(context.session_factory) as uow:
            candidates = await uow.relations.list_missing_embedding_candidates(
                dataset_id=dataset_id
            )

        if not candidates:
            self._staged.stage(context.run_id, {})
            return StepResult(output={"relations_embedded": 0})

        texts = [relation_embedding_text(candidate) for candidate in candidates]
        embeddings = await _embed(resources, texts, subject="Relation")
        validate_embedding_response(
            embeddings,
            expected_count=len(candidates),
            expected_dimensions=resources.settings.embedding_dimensions,
            subject="Relation",
        )
        embeddings_by_id = {
            candidate.relation_id: list(embedding)
            for candidate, embedding in zip(candidates, embeddings, strict=True)
        }
        self._staged.stage(context.run_id, embeddings_by_id)
        return StepResult(output={"relations_embedded": len(embeddings_by_id)})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        embeddings_by_id = self._staged.pop(context.run_id)
        if embeddings_by_id is None:
            raise PermanentPipelineStepError(
                IMPROVE_BATCH_MISSING_ERROR_CODE, IMPROVE_BATCH_MISSING_MESSAGE
            )
        if not embeddings_by_id:
            return
        actual = await uow.relations.set_missing_embeddings_for_active_current(
            dataset_id=_dataset_id(context),
            embeddings_by_relation_id=embeddings_by_id,
        )
        result.output["relations_embedded"] = actual

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id)
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# summaries -- staged-batch split (single step, both halves).
# ---------------------------------------------------------------------------


class SummariesStep:
    """ATOMIC: ``execute`` (``SummaryRebuildService.prepare_dataset``) does
    every LLM/embedding call and writes nothing; ``persist``
    (``apply_prepared_summaries``) is PostgreSQL-only."""

    def __init__(self) -> None:
        self._staged: _StagedBatchCache[PreparedSummaryRebuild] = _StagedBatchCache()

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)
        generation = await _active_generation(context, dataset_id)
        if generation is None:
            self._staged.drop(context.run_id)
            return StepResult(
                output={
                    "prepared": False,
                    "document_summaries_rebuilt": 0,
                    "dataset_summaries_rebuilt": 0,
                    "summaries_deactivated": 0,
                }
            )
        try:
            prepared = await resources.summary_rebuild.prepare_dataset(
                dataset_id, generation=generation
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider/storage failure classification
            raise RetryablePipelineStepError(
                IMPROVE_DEPENDENCY_ERROR_CODE, IMPROVE_DEPENDENCY_ERROR_MESSAGE
            ) from exc
        self._staged.stage(context.run_id, prepared)
        return StepResult(output={"prepared": True})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        if result.output.get("prepared") is not True:
            # No eligible generation snapshot at execute time -- nothing staged.
            return
        prepared = self._staged.pop(context.run_id)
        if prepared is None:
            raise PermanentPipelineStepError(
                IMPROVE_BATCH_MISSING_ERROR_CODE, IMPROVE_BATCH_MISSING_MESSAGE
            )
        resources_settings = _resources(context).settings
        counts = await apply_prepared_summaries(
            cast(Any, uow), prepared, settings=resources_settings
        )
        result.output.update(
            {
                "document_summaries_rebuilt": counts.document_summaries_rebuilt,
                "dataset_summaries_rebuilt": counts.dataset_summaries_rebuilt,
                "summaries_deactivated": counts.summaries_deactivated,
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id)
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# graph_reconciliation (pre) -- drains this run's own prior slots, reconciles.
# ---------------------------------------------------------------------------


class GraphReconcileStep:
    """ATOMIC (vacuously): this step never commits an authoritative
    PostgreSQL mutation of its own -- Neo4j drift repair is projection
    maintenance, not Improve's own business state, and both the pre-drain
    and the reconciliation it performs are independently idempotent/safe to
    redo (SM-506's leasing, and reconciliation's own read-compare-repair
    design). An orphaned RUNNING row therefore needs no durable-evidence
    check either way."""

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)
        if resources.graph_reconciliation is None or resources.graph_outbox_drain is None:
            raise PermanentPipelineStepError(
                IMPROVE_RESOURCE_MISSING_ERROR_CODE, IMPROVE_RESOURCE_MISSING_MESSAGE
            )

        # SM-511 SS 21: converge every EARLIER slot's own graph_outbox writes
        # (from whichever public stages the request placed before this one --
        # this slot's public position is honored, not a canonical position)
        # before comparing against Neo4j, so reconciliation never judges
        # drift against a stale snapshot of this run's own prior work.
        pre_drain = await resources.graph_outbox_drain.process_dataset(dataset_id)
        try:
            reconciliation = await resources.graph_reconciliation.reconcile_dataset(dataset_id)
        except DependencyUnavailableError as exc:
            raise RetryablePipelineStepError(
                IMPROVE_DEPENDENCY_ERROR_CODE, IMPROVE_DEPENDENCY_ERROR_MESSAGE
            ) from exc

        diff = reconciliation.diff
        return StepResult(
            output={
                "graph_events_processed_pre_drain": pre_drain.processed,
                "entities_missing": diff.entities_missing,
                "entities_extra": diff.entities_extra,
                "chunks_missing": diff.chunks_missing,
                "chunks_extra": diff.chunks_extra,
                "entity_mentions_missing": diff.entity_mentions_missing,
                "entity_mentions_extra": diff.entity_mentions_extra,
                "relations_missing": diff.relations_missing,
                "relations_extra": diff.relations_extra,
                "next_missing": diff.next_missing,
                "next_extra": diff.next_extra,
                "rebuilt": reconciliation.rebuilt,
            }
        )

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# graph_reconciliation (main) -- PostgreSQL-only maintenance.
# ---------------------------------------------------------------------------


class GraphMaintainStep:
    """ATOMIC: pure PostgreSQL (centrality/importance/hygiene), so the whole
    compute+apply sequence lives in ``persist``, reusing
    ``compute_maintenance_plan_with_uow``/``apply_maintenance_plan`` against
    the engine's own unit of work."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        dataset = await uow.datasets.get_by_id(dataset_id)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            result.output.update(
                {
                    "relations_deactivated": 0,
                    "entities_importance_updated": 0,
                    "relations_importance_updated": 0,
                    "graph_events_enqueued": 0,
                }
            )
            return
        plan = await compute_maintenance_plan_with_uow(
            cast(Any, uow), dataset_id=dataset_id, generation=dataset.active_generation
        )
        if plan is None:
            result.output.update(
                {
                    "relations_deactivated": 0,
                    "entities_importance_updated": 0,
                    "relations_importance_updated": 0,
                    "graph_events_enqueued": 0,
                }
            )
            return
        counts = await apply_maintenance_plan(cast(Any, uow), plan)
        result.output.update(
            {
                "relations_deactivated": counts.relations_deactivated,
                "entities_importance_updated": counts.entities_importance_updated,
                "relations_importance_updated": counts.relations_importance_updated,
                "graph_events_enqueued": counts.graph_events_enqueued,
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# graph_reconciliation (post) -- converge what maintenance enqueued.
# ---------------------------------------------------------------------------


class GraphDrainStep:
    """ATOMIC (vacuously): converges whatever ``graph_maintain`` enqueued,
    so the compound ``graph_reconciliation`` stage is fully converged by the
    time it concludes (SM-511 MAJOR 2 item J), independent of the also-always
    -present ``final_convergence`` step. Draining is itself an
    already-idempotent, independently leased operation (SM-506) -- not an
    authoritative mutation this step owns."""

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)
        if resources.graph_outbox_drain is None:
            raise PermanentPipelineStepError(
                IMPROVE_RESOURCE_MISSING_ERROR_CODE, IMPROVE_RESOURCE_MISSING_MESSAGE
            )
        drained = await resources.graph_outbox_drain.process_dataset(dataset_id)
        return StepResult(output={"graph_events_processed": drained.processed})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# Slot dispatch (SM-511 MAJOR 1): the ONLY place that reads "which stage is
# in this slot" and decides whether a phase handler runs at all.
# ---------------------------------------------------------------------------

PHASE_HANDLERS: dict[str, dict[str, PipelineStep]] = {
    FEEDBACK_WEIGHTS_STAGE: {MAIN_PHASE: FeedbackWeightsStep()},
    ENTITY_DEDUPLICATION_STAGE: {
        PRE_PHASE: EntityEmbeddingsStep(),
        MAIN_PHASE: EntityMergeStep(),
    },
    RELATION_EMBEDDINGS_STAGE: {MAIN_PHASE: RelationEmbeddingsStep()},
    SUMMARIES_STAGE: {MAIN_PHASE: SummariesStep()},
    GRAPH_RECONCILIATION_STAGE: {
        PRE_PHASE: GraphReconcileStep(),
        MAIN_PHASE: GraphMaintainStep(),
        POST_PHASE: GraphDrainStep(),
    },
}
"""Closed, code-defined dispatch table (ADR-0009 SS O): exactly the five
approved public stages, each mapped to its fixed phase handler instance(s).
Never mutated, never resolved dynamically from request data -- a request can
only ever select from :data:`SUPPORTED_IMPROVE_STAGES`
(``services.improve``), which is exactly this dict's key set."""


class SlotStep:
    """One (slot_index, phase) position in the static pipeline. Resolves,
    from ``run_input`` alone, whether a real handler occupies this position
    for this run and -- if so -- delegates to it verbatim; otherwise this
    position is a true no-op (SM-511 SS 32 #16). The resolution is pure and
    reproduced identically in ``execute`` and ``persist`` (both derive it
    fresh from ``context.run_input``), so it can never disagree with itself
    mid-attempt."""

    def __init__(self, slot_index: int, phase: str) -> None:
        self._slot_index = slot_index
        self._phase = phase

    def _handler(self, run_input: Mapping[str, Any]) -> PipelineStep | None:
        stages = resolve_slot_stages(run_input)
        stage = stages[self._slot_index]
        if stage is None:
            return None
        return PHASE_HANDLERS.get(stage, {}).get(self._phase)

    async def execute(self, context: PipelineContext) -> StepResult:
        handler = self._handler(context.run_input)
        if handler is None:
            return StepResult(output={})
        return await handler.execute(context)

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        handler = self._handler(context.run_input)
        if handler is None:
            return
        await handler.persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        handler = self._handler(context.run_input)
        if handler is None:
            no_op_compensate(context, result)
            return
        await handler.compensate(context, result)


# ---------------------------------------------------------------------------
# final_convergence -- SM-511 MAJOR 2: unconditional convergence barrier.
# ---------------------------------------------------------------------------


class FinalConvergenceStep:
    """Always present, always executed, regardless of which stages (if any)
    were selected. Drains any ``graph_outbox`` rows still outstanding for
    this dataset -- whether enqueued by ``feedback_weights``,
    ``entity_merge``, or ``graph_maintain`` -- so the run can never report
    ``SUCCEEDED`` while its own projection work is known-pending. Runs after
    every slot has committed (ordinal position: last, before
    ``finalize_result``), so it observes every graph_outbox row this run's
    own selected stages produced.

    ATOMIC (vacuously): no authoritative PostgreSQL mutation of its own --
    draining is independently idempotent (SM-506 leasing), safe to redo on
    retry/resume without risk of duplicating any business effect.

    Degrades to a documented no-op when the process has no Neo4j resource at
    all (``resources.graph_outbox_drain is None``): an Improve run in a
    Neo4j-disabled environment cannot converge anything regardless, and
    failing every such run outright (including feedback-only requests that
    never asked for graph reconciliation) would be a strictly worse outcome
    than reporting zero convergence work.
    """

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)
        if resources.graph_outbox_drain is None:
            return StepResult(output={"graph_events_processed": 0})
        drained = await resources.graph_outbox_drain.process_dataset(dataset_id)
        return StepResult(output={"graph_events_processed": drained.processed})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# finalize_result -- aggregate every prior step's safe output.
# ---------------------------------------------------------------------------


class FinalizeResultStep:
    """ATOMIC: pure aggregation of already-safe counts from
    ``context.step_outputs`` plus one dataset read; writes
    ``run.metrics[IMPROVE_RESULT_METRIC_KEY]`` in the engine's own
    transaction (mirrors Cognify's ``ActivateGenerationStep``). Locates each
    stage's output by re-resolving ``resolve_slot_stages`` (the same pure
    function ``SlotStep`` used), never by a fixed step name -- a stage's
    slot index varies per request (SM-511 MAJOR 1)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        dataset = await uow.datasets.get_by_id(dataset_id)
        if dataset is None:
            raise PermanentPipelineStepError(
                IMPROVE_ACTIVATION_ERROR_CODE, IMPROVE_ACTIVATION_ERROR_MESSAGE
            )

        stages = resolve_slot_stages(context.run_input)

        def output_for(stage: str, phase: str) -> Mapping[str, Any]:
            if stage not in stages:
                return {}
            slot_index = stages.index(stage)
            return context.step_outputs.get(slot_step_name(slot_index, phase), {})

        feedback = output_for(FEEDBACK_WEIGHTS_STAGE, MAIN_PHASE)
        entity_embeddings = output_for(ENTITY_DEDUPLICATION_STAGE, PRE_PHASE)
        entity_merge = output_for(ENTITY_DEDUPLICATION_STAGE, MAIN_PHASE)
        relation_embeddings = output_for(RELATION_EMBEDDINGS_STAGE, MAIN_PHASE)
        summaries = output_for(SUMMARIES_STAGE, MAIN_PHASE)
        reconcile = output_for(GRAPH_RECONCILIATION_STAGE, PRE_PHASE)
        maintain = output_for(GRAPH_RECONCILIATION_STAGE, MAIN_PHASE)
        drain = output_for(GRAPH_RECONCILIATION_STAGE, POST_PHASE)
        final_convergence = context.step_outputs.get(FINAL_CONVERGENCE_STEP, {})
        normalized_stages = [stage for stage in stages if stage is not None]

        graph_events_enqueued = (
            int(feedback.get("graph_events_enqueued", 0) or 0)
            + int(entity_merge.get("graph_events_enqueued", 0) or 0)
            + int(maintain.get("graph_events_enqueued", 0) or 0)
        )
        # SM-511 MAJOR 2: deterministic, PostgreSQL-authoritative semantics
        # -- the sum of every convergence barrier THIS run itself performed
        # (pre-reconcile drain, post-maintain drain, and the always-present
        # final barrier). Each barrier call only "claims credit" for rows
        # still outstanding at ITS OWN snapshot; a row the autonomous SM-506
        # consumer already finished before that snapshot is not double
        # counted (it is simply absent from the snapshot, already
        # converged). This is not "how many rows this call raced to claim"
        # -- every barrier blocks until every snapshotted row reaches a
        # terminal state, so the sum is a safe lower bound on total
        # convergence work this run observed, never an inflated or
        # racy count.
        graph_events_processed = (
            int(reconcile.get("graph_events_processed_pre_drain", 0) or 0)
            + int(drain.get("graph_events_processed", 0) or 0)
            + int(final_convergence.get("graph_events_processed", 0) or 0)
        )

        run_result: dict[str, Any] = {
            "dataset_id": str(dataset_id),
            "generation": dataset.active_generation,
            "stages": normalized_stages,
            "feedback_processed": int(feedback.get("processed", 0) or 0),
            "feedback_applied": int(feedback.get("applied", 0) or 0),
            "feedback_skipped": int(feedback.get("skipped", 0) or 0),
            "entities_updated": int(feedback.get("entities_updated", 0) or 0),
            "relations_updated": int(feedback.get("relations_updated", 0) or 0),
            "relations_embedded": int(relation_embeddings.get("relations_embedded", 0) or 0),
            "entities_embedded": int(entity_embeddings.get("entities_embedded", 0) or 0),
            "entity_duplicate_candidates": int(entity_merge.get("duplicate_candidates", 0) or 0),
            "entities_merged": int(entity_merge.get("entities_merged", 0) or 0),
            "entity_mentions_reassigned": int(
                entity_merge.get("entity_mentions_reassigned", 0) or 0
            ),
            "relations_rewired": int(entity_merge.get("relations_rewired", 0) or 0),
            "relations_deactivated": int(entity_merge.get("relations_deactivated", 0) or 0),
            "relation_evidence_copied": int(entity_merge.get("relation_evidence_copied", 0) or 0),
            "document_summaries_rebuilt": int(summaries.get("document_summaries_rebuilt", 0) or 0),
            "dataset_summaries_rebuilt": int(summaries.get("dataset_summaries_rebuilt", 0) or 0),
            "summaries_deactivated": int(summaries.get("summaries_deactivated", 0) or 0),
            "graph_relations_deactivated": int(maintain.get("relations_deactivated", 0) or 0),
            "graph_entities_importance_updated": int(
                maintain.get("entities_importance_updated", 0) or 0
            ),
            "graph_relations_importance_updated": int(
                maintain.get("relations_importance_updated", 0) or 0
            ),
            "graph_entities_missing": int(reconcile.get("entities_missing", 0) or 0),
            "graph_entities_extra": int(reconcile.get("entities_extra", 0) or 0),
            "graph_chunks_missing": int(reconcile.get("chunks_missing", 0) or 0),
            "graph_chunks_extra": int(reconcile.get("chunks_extra", 0) or 0),
            "graph_entity_mentions_missing": int(reconcile.get("entity_mentions_missing", 0) or 0),
            "graph_entity_mentions_extra": int(reconcile.get("entity_mentions_extra", 0) or 0),
            "graph_relations_missing": int(reconcile.get("relations_missing", 0) or 0),
            "graph_relations_extra": int(reconcile.get("relations_extra", 0) or 0),
            "graph_next_missing": int(reconcile.get("next_missing", 0) or 0),
            "graph_next_extra": int(reconcile.get("next_extra", 0) or 0),
            "graph_rebuilt": bool(reconcile.get("rebuilt", False)),
            "graph_events_enqueued": graph_events_enqueued,
            "graph_events_processed": graph_events_processed,
        }
        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                IMPROVE_ACTIVATION_ERROR_CODE, IMPROVE_ACTIVATION_ERROR_MESSAGE
            )
        run.metrics = {**run.metrics, IMPROVE_RESULT_METRIC_KEY: run_result}
        run.dataset_id = dataset_id

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


async def _active_generation(context: PipelineContext, dataset_id: UUID) -> int | None:
    async with PostgresUnitOfWork(context.session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            return None
        return dataset.active_generation


async def _embed(
    resources: ImprovePipelineResources, texts: list[str], *, subject: str
) -> list[list[float]]:
    del subject
    try:
        return await resources.embedding_client.embed_texts(texts)
    except DependencyUnavailableError as exc:
        raise RetryablePipelineStepError(
            IMPROVE_DEPENDENCY_ERROR_CODE, IMPROVE_DEPENDENCY_ERROR_MESSAGE
        ) from exc
    except Exception as exc:  # noqa: BLE001 - provider failure classification
        raise RetryablePipelineStepError(
            IMPROVE_DEPENDENCY_ERROR_CODE, IMPROVE_DEPENDENCY_ERROR_MESSAGE
        ) from exc


def _uniform_input(
    run_input: Mapping[str, Any], step_outputs: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Every step's semantic input is derivable purely from ``run_input``
    (dataset + normalized, order-preserving stage list) -- none of these
    steps' *input hash* depends on a prior step's output, even though some
    read ``context.step_outputs`` at runtime (``finalize_result``) or
    execute in a fixed sequence after another step's commit (a slot's
    ``main``/``post`` after its own ``pre``). Ordinal ordering in the
    ``PipelineDefinition`` -- not input-hash dependency -- is what
    guarantees that sequencing (ADR-0009 SS O)."""

    del step_outputs
    return {"dataset": run_input.get("dataset"), "stages": run_input.get("stages")}


def build_improve_pipeline_definition() -> PipelineDefinition:
    """The single registered Improve pipeline (SM-511): five fixed slots x
    three fixed phases, plus the two always-present closing steps. Exactly
    17 steps, always, regardless of what any request selects."""

    def step_def(name: str, step: PipelineStep) -> PipelineStepDefinition:
        return PipelineStepDefinition(
            name=name,
            definition_id=f"{_DEFINITION_ID_PREFIX}{name}.v1",
            step=step,
            input_deriver=_uniform_input,
            # ATOMIC: every concrete handler behind every slot is itself
            # ATOMIC (see each class's own docstring for why); a no-op slot
            # position commits nothing either. SlotStep's dispatch is pure
            # and side-effect-free, so it introduces no new recovery risk.
            cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
        )

    slot_steps = tuple(
        step_def(slot_step_name(slot_index, phase), SlotStep(slot_index, phase))
        for slot_index in range(SLOT_COUNT)
        for phase in (PRE_PHASE, MAIN_PHASE, POST_PHASE)
    )

    return PipelineDefinition(
        pipeline_type=PipelineType.IMPROVE,
        steps=(
            *slot_steps,
            step_def(FINAL_CONVERGENCE_STEP, FinalConvergenceStep()),
            step_def(FINALIZE_RESULT_STEP, FinalizeResultStep()),
        ),
    )


__all__ = [
    "FINALIZE_RESULT_STEP",
    "FINAL_CONVERGENCE_STEP",
    "IMPROVE_RESOURCES_RESOURCE",
    "MAIN_PHASE",
    "PHASE_HANDLERS",
    "POST_PHASE",
    "PRE_PHASE",
    "SLOT_COUNT",
    "EntityEmbeddingsStep",
    "EntityMergeStep",
    "FeedbackWeightsStep",
    "FinalConvergenceStep",
    "FinalizeResultStep",
    "GraphDrainStep",
    "GraphMaintainStep",
    "GraphReconcileStep",
    "ImprovePipelineResources",
    "RelationEmbeddingsStep",
    "SlotStep",
    "SummariesStep",
    "build_improve_pipeline_definition",
    "resolve_slot_stages",
    "slot_step_name",
]
