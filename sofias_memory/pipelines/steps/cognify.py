"""Cognify pipeline steps (SM-510, ADR-0009 SS O).

Two steps, in order:

1. ``process_sources`` -- external/computation in ``execute``, PostgreSQL-only
   in ``persist``. ``execute`` runs B4's unchanged chunking/embedding/
   extraction/summary algorithms against the *target* generation and resolves
   every canonical entity/relation identity, but writes nothing: it returns an
   in-memory ``CognifyPreparedBatch``. ``persist`` applies that whole batch in
   the engine's own transaction, so the step's authoritative rows and its
   ``PipelineStep.status = succeeded`` transition land in exactly one commit.
   Nothing a crash between the two phases leaves behind is durable.
2. ``activate_generation`` -- pure in ``execute``, PostgreSQL-only in
   ``persist``. For ``rebuild=true`` it flips ``Dataset.active_generation``
   from ``N`` to ``N+1`` inside the engine's own transaction, together with
   the step's ``succeeded`` transition, so a partially-built generation is
   never observable and a crash before that commit leaves ``N`` intact.
   For ``rebuild=false`` it only records the business result.

Neither step drains the ``graph_outbox``: emitting the outbox rows is itself
an authoritative mutation that only exists once ``persist`` commits, and
``persist`` may never call Neo4j. Projection therefore converges through
SM-506's autonomous ``graph_outbox`` consumer, independently of this pipeline.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.domain import PipelineType
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import (
    PermanentPipelineStepError,
    RetryablePipelineStepError,
)
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    PipelineDefinition,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
)
from sofias_memory.ports import entity_upsert_command
from sofias_memory.services.cognify import (
    COGNIFY_RESULT_METRIC_KEY,
    CognifyPreparedBatch,
    CognifyProcessOutcome,
    CognifyUnitOfWork,
    cognify_source_ids_from_run_input,
)

COGNIFY_SERVICE_RESOURCE = "cognify_service"
"""``PipelineContext.resources`` key holding the process-wide
:class:`~sofias_memory.services.cognify.CognifyService` (and, through it, the
LLM/embedding/summary clients). Built once at
application startup and handed to the engine -- never constructed per request
and never resolved dynamically from anything a request controls."""

PROCESS_SOURCES_STEP = "process_sources"
ACTIVATE_GENERATION_STEP = "activate_generation"

PROCESS_SOURCES_DEFINITION_ID = "cognify.process_sources.v1"
ACTIVATE_GENERATION_DEFINITION_ID = "cognify.activate_generation.v1"

COGNIFY_DEPENDENCY_ERROR_CODE = "COGNIFY_DEPENDENCY_UNAVAILABLE"
COGNIFY_REQUEST_ERROR_CODE = "COGNIFY_REQUEST_REJECTED"
COGNIFY_RESOURCE_MISSING_ERROR_CODE = "COGNIFY_RESOURCE_MISSING"
COGNIFY_SCOPE_ERROR_CODE = "COGNIFY_RUN_SCOPE_INVALID"
COGNIFY_ACTIVATION_ERROR_CODE = "COGNIFY_ACTIVATION_TARGET_MISSING"
COGNIFY_BATCH_MISSING_ERROR_CODE = "COGNIFY_PREPARED_BATCH_MISSING"

COGNIFY_DEPENDENCY_ERROR_MESSAGE = "A Cognify dependency was unavailable."
COGNIFY_REQUEST_ERROR_MESSAGE = "Cognify could not process the requested work."
COGNIFY_RESOURCE_MISSING_MESSAGE = "Cognify processing resources are not configured."
COGNIFY_SCOPE_ERROR_MESSAGE = "Cognify run is not scoped to a dataset."
COGNIFY_ACTIVATION_ERROR_MESSAGE = "Cognify generation activation target no longer exists."
COGNIFY_BATCH_MISSING_MESSAGE = "Cognify prepared batch is not available for this run."

STAGED_BATCH_MAX_AGE_SECONDS = 900.0
"""Upper bound on how long a staged (computed-but-not-yet-persisted) batch may
sit in :class:`ProcessSourcesStep`'s private cache before it is treated as
abandoned and dropped.

A staged entry is created at the very end of ``execute`` and consumed by the
``persist`` the engine invokes in the immediately following statement, so its
real lifetime is one short transaction. The one path that leaves an entry
behind is ownership fencing: when ``PipelineEngine._commit_success`` finds
``_load_fenced_running_step`` returning ``None`` (this worker was superseded
mid-attempt), it returns ``abandoned=True`` and never calls ``persist``. This
bound keeps that case from accumulating chunk text/embeddings in a long-lived
worker process. It is a memory hygiene bound only -- never a correctness one:
the batch is transient, recomputable state, and a fresh attempt's ``execute``
rebuilds it from scratch."""


class CognifySourceProcessor(Protocol):
    """The only capability this step needs from the injected resource --
    implemented by :class:`~sofias_memory.services.cognify.CognifyService`,
    and by an in-memory double in tests (the same fake-vs-real split used
    throughout the B5 services). Deliberately two methods, one per ADR-0009
    SS O phase: nothing here can compute and commit in one call."""

    async def prepare_batch(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID] | None,
        rebuild: bool,
    ) -> CognifyPreparedBatch: ...

    async def persist_batch(
        self,
        uow: CognifyUnitOfWork,
        batch: CognifyPreparedBatch,
    ) -> CognifyProcessOutcome: ...


def _rebuild_flag(run_input: Mapping[str, Any]) -> bool:
    return run_input.get("rebuild") is True


def _outcome_output(outcome: CognifyProcessOutcome) -> dict[str, Any]:
    """The JSON-safe business result (ADR-0009 SS 10): counts and identities
    only -- never chunk text, an embedding, a prompt, or a provider payload."""

    return {
        "dataset_id": str(outcome.dataset_id),
        "target_generation": outcome.target_generation,
        "rebuild": outcome.rebuild,
        "sources_processed": outcome.sources_processed,
        "chunks": outcome.chunks,
        "entities": outcome.entities,
        "relations": outcome.relations,
    }


class ProcessSourcesStep:
    """B4's per-source pipeline, split across the two ADR-0009 SS O phases.

    ``execute`` computes; ``persist`` writes. The computed batch is handed
    between them through a private, process-local cache on this step
    instance, keyed by ``PipelineContext.run_id``. That is sound precisely
    because the engine calls ``persist(context, result, uow)`` in the very
    next statement after this same attempt's ``execute`` returned
    successfully, in the same coroutine (``PipelineEngine._run_step`` ->
    ``_persist_step_outcome`` -> ``_commit_success``), and ownership fencing
    guarantees at most one live attempt per run. The batch deliberately never
    travels through ``StepResult``/``PipelineContext``: both are persisted or
    shared engine-core types, and the batch holds chunk text, embeddings and
    extracted knowledge that must never reach a ``PipelineStep`` row.

    The cache is transient, recomputable state and never a source of truth:
    losing it (crash, worker kill, cancellation, this class's own age-based
    sweep) costs at most redoing ``execute``'s external work on the next
    attempt, never correctness -- nothing durable ever depended on it.
    """

    def __init__(self) -> None:
        self._staged: dict[UUID, tuple[float, CognifyPreparedBatch]] = {}

    async def execute(self, context: PipelineContext) -> StepResult:
        service = self._service(context)
        if context.dataset_id is None:
            raise PermanentPipelineStepError(COGNIFY_SCOPE_ERROR_CODE, COGNIFY_SCOPE_ERROR_MESSAGE)

        try:
            batch = await service.prepare_batch(
                dataset_id=context.dataset_id,
                source_ids=cognify_source_ids_from_run_input(context.run_input),
                rebuild=_rebuild_flag(context.run_input),
            )
        except DependencyUnavailableError as exc:
            # Provider/storage unavailability is the one genuinely transient
            # class here -- worth another attempt with backoff. Only the
            # stable code and a safe message are ever persisted.
            raise RetryablePipelineStepError(
                COGNIFY_DEPENDENCY_ERROR_CODE, COGNIFY_DEPENDENCY_ERROR_MESSAGE
            ) from exc
        except SofiasMemoryError as exc:
            raise PermanentPipelineStepError(
                COGNIFY_REQUEST_ERROR_CODE, COGNIFY_REQUEST_ERROR_MESSAGE
            ) from exc

        # A superseded attempt's entry is simply overwritten: the run id is
        # the key, and only one attempt of a run is ever live. An entry left
        # behind by an attempt the engine fenced out before ``persist`` (and
        # whose run never comes back to this process) is dropped by the sweep.
        self._evict_abandoned()
        self._staged[context.run_id] = (time.monotonic(), batch)
        return StepResult(output=_outcome_output(batch.planned_outcome()))

    def _evict_abandoned(self) -> None:
        cutoff = time.monotonic() - STAGED_BATCH_MAX_AGE_SECONDS
        for run_id in [
            run_id for run_id, (staged_at, _) in self._staged.items() if staged_at < cutoff
        ]:
            del self._staged[run_id]

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        staged = self._staged.pop(context.run_id, None)
        if staged is None:
            # Structurally impossible with the current engine (persist is only
            # ever reached immediately after this same attempt's execute
            # succeeded). Fail loudly rather than no-op into a silently empty
            # cognify or recompute external work inside a transaction.
            raise PermanentPipelineStepError(
                COGNIFY_BATCH_MISSING_ERROR_CODE, COGNIFY_BATCH_MISSING_MESSAGE
            )
        _, batch = staged
        service = self._service(context)
        outcome = await service.persist_batch(cast(CognifyUnitOfWork, uow), batch)
        # The engine writes ``result.output`` into ``PipelineStep.output``
        # only *after* this returns, so correcting the counters here keeps
        # the durable record truthful when per-source isolation dropped a
        # source that the plan had counted.
        result.output.update(_outcome_output(outcome))

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id, None)
        no_op_compensate(context, result)

    @staticmethod
    def _service(context: PipelineContext) -> CognifySourceProcessor:
        resource = context.resources.get(COGNIFY_SERVICE_RESOURCE)
        if resource is None:
            raise PermanentPipelineStepError(
                COGNIFY_RESOURCE_MISSING_ERROR_CODE, COGNIFY_RESOURCE_MISSING_MESSAGE
            )
        return cast(CognifySourceProcessor, resource)


class ActivateGenerationStep:
    """Atomic generation flip (rebuild only) plus durable result recording."""

    async def execute(self, context: PipelineContext) -> StepResult:
        upstream = context.step_outputs.get(PROCESS_SOURCES_STEP)
        if upstream is None:
            raise PermanentPipelineStepError(
                COGNIFY_ACTIVATION_ERROR_CODE, COGNIFY_ACTIVATION_ERROR_MESSAGE
            )
        # Pure pass-through: no I/O at all, so there is nothing this step can
        # leave half-done outside the transaction its ``persist`` runs in.
        return StepResult(output=dict(upstream))

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        if context.dataset_id is None:
            raise PermanentPipelineStepError(COGNIFY_SCOPE_ERROR_CODE, COGNIFY_SCOPE_ERROR_MESSAGE)
        target_generation = int(result.output["target_generation"])
        rebuild = result.output.get("rebuild") is True

        dataset = await uow.datasets.get_by_id_for_update(context.dataset_id)
        if dataset is None:
            raise PermanentPipelineStepError(
                COGNIFY_ACTIVATION_ERROR_CODE, COGNIFY_ACTIVATION_ERROR_MESSAGE
            )

        if rebuild and dataset.active_generation < target_generation:
            # Idempotent guard: a crash between this commit and the engine
            # marking the step succeeded replays this step, and the second
            # pass must not advance the generation a second time.
            for entity in await uow.entities.advance_active_generation(
                dataset_id=dataset.id,
                target_generation=target_generation,
            ):
                # Same transaction as the mutation (AGENTS SS 15): the graph
                # projection of a carried-forward entity is re-emitted with
                # its new generation, so Neo4j converges without a rebuild.
                await uow.graph_outbox.add_projection_command(
                    entity_upsert_command(
                        entity_id=entity.id,
                        dataset_id=entity.dataset_id,
                        name=entity.name,
                        entity_type=entity.entity_type,
                        description=entity.description,
                        importance_weight=float(entity.importance_weight),
                        generation=entity.generation,
                    )
                )
            dataset.active_generation = target_generation

        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                COGNIFY_ACTIVATION_ERROR_CODE, COGNIFY_ACTIVATION_ERROR_MESSAGE
            )
        run.metrics = {
            **run.metrics,
            COGNIFY_RESULT_METRIC_KEY: {
                "dataset_id": str(dataset.id),
                "generation": target_generation,
                "sources_processed": int(result.output["sources_processed"]),
                "chunks": int(result.output["chunks"]),
                "entities": int(result.output["entities"]),
                "relations": int(result.output["relations"]),
            },
        }

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def process_sources_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Fully derivable from ``run_input`` alone, so its hash is persisted at
    submission time. ``wait`` is not part of ``run_input`` and can therefore
    never influence it."""

    del step_outputs
    return {
        "dataset": run_input.get("dataset"),
        "source_ids": run_input.get("source_ids"),
        "rebuild": run_input.get("rebuild"),
    }


def activate_generation_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Depends on the prior step's output, so it is ``None`` (not yet
    computable) at submission time and backfilled at this step's own
    checkpoint (ADR-0009 SS 12)."""

    del run_input
    upstream = step_outputs.get(PROCESS_SOURCES_STEP)
    if upstream is None:
        return None
    return {
        "target_generation": upstream.get("target_generation"),
        "rebuild": upstream.get("rebuild"),
    }


def build_cognify_pipeline_definition() -> PipelineDefinition:
    """The single registered Cognify pipeline (SM-510)."""

    return PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY,
        steps=(
            PipelineStepDefinition(
                name=PROCESS_SOURCES_STEP,
                definition_id=PROCESS_SOURCES_DEFINITION_ID,
                step=ProcessSourcesStep(),
                input_deriver=process_sources_input,
                # ATOMIC: ``execute`` commits nothing -- it only computes and
                # stages an in-memory batch -- and ``persist`` applies every
                # authoritative mutation inside the engine's own transaction,
                # in the same commit as this step's ``succeeded`` transition.
                # An orphaned RUNNING row is therefore proof that nothing was
                # committed, so recovery needs no durable-evidence check.
                cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
            ),
            PipelineStepDefinition(
                name=ACTIVATE_GENERATION_STEP,
                definition_id=ACTIVATE_GENERATION_DEFINITION_ID,
                step=ActivateGenerationStep(),
                input_deriver=activate_generation_input,
                # ATOMIC: this step's only mutation commits together with its
                # own succeeded transition, so an orphaned RUNNING row is
                # proof that nothing was committed.
                cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
            ),
        ),
    )


__all__ = [
    "ACTIVATE_GENERATION_DEFINITION_ID",
    "ACTIVATE_GENERATION_STEP",
    "COGNIFY_SERVICE_RESOURCE",
    "PROCESS_SOURCES_DEFINITION_ID",
    "PROCESS_SOURCES_STEP",
    "STAGED_BATCH_MAX_AGE_SECONDS",
    "ActivateGenerationStep",
    "ProcessSourcesStep",
    "activate_generation_input",
    "build_cognify_pipeline_definition",
    "process_sources_input",
]
