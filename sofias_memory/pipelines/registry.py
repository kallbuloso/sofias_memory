"""Closed, code-defined pipeline registry (ADR-0009 SS O).

No pipeline is ever accepted from a client request, no dynamic import is
driven by input, and there is no plugin/registration system: a
:class:`PipelineRegistry` is built once, from a fixed collection of
:class:`PipelineDefinition` objects, and never mutated afterward. This is
also the single place that knows how to turn a ``PipelineType`` into the
``StepPlan`` list a durable submission (``create_run_with_steps``, SM-502)
persists -- so "the steps the engine will execute" and "the steps a
submission materializes" can never silently drift apart into two separate
definitions (SM-504 story requirement).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sofias_memory.domain import PipelineType
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.hashing import canonical_step_input_hash


@dataclass(frozen=True, slots=True)
class StepResult:
    """What a step returns on success. JSON-safe only (ADR-0009 SS 10):
    never a raw provider response, document text, embedding, prompt,
    secret, or traceback."""

    output: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class PipelineStep(Protocol):
    """ADR-0009 SS O/SS 9 step contract -- three phases, not two.

    ``execute`` is the external/computation phase: it runs outside any held
    lock or long transaction and may call an LLM, embedding provider, HTTP,
    or the filesystem. ``persist`` is the short, PostgreSQL-only
    transactional phase the *engine* calls, inside the exact transaction it
    already holds to finalize the step -- this is the amendment ADR-0009 SS
    O records for SM-504: without it, a step's own authoritative mutation
    and its ``PipelineStep`` row reaching ``succeeded`` could never land in
    the same commit (the "Commit boundary preference"), because ``execute``
    itself must never hold a transaction across an external call. The
    engine, never the step, owns every ``PipelineRun``/``PipelineStep``
    lifecycle transition.
    """

    async def execute(self, context: PipelineContext) -> StepResult:
        """External/computation phase. Never opens a transaction, never
        transitions ``PipelineRun``/``PipelineStep``."""
        ...

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        """Apply this step's own authoritative PostgreSQL mutation, if any,
        using the engine-supplied, already-open ``uow`` -- the same
        transaction the engine commits the step's own ``succeeded``
        transition in immediately afterward. Never calls ``commit``, never
        opens an independent transaction, never calls an LLM/embedding/
        HTTP/Neo4j dependency. A step with no authoritative mutation of its
        own implements this as a no-op (:func:`no_op_persist`)."""
        ...

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        """Exceptional tool for a step whose partial artifact truly cannot be
        left abandoned safely (ADR-0009 SS O). Never invoked automatically by
        retry or cancellation -- replay-safety relies on idempotency/
        reconciliation (forward replay), not rollback. Most steps implement
        this as a no-op."""
        ...


InputDeriver = Callable[
    [Mapping[str, Any], Mapping[str, Mapping[str, Any]]],
    Mapping[str, Any] | None,
]
"""``(run_input, step_outputs_by_name) -> semantic_input | None``.

Deliberately takes plain mappings, not a full :class:`PipelineContext` --
input-hash derivation must stay pure and callable both at submission time
(before a run/session exists) and mid-execution (ADR-0009 SS 11/SS 12).
Returns ``None`` when the step's input is not yet computable (it depends on
a prior step's output that has not succeeded yet); the engine computes and
persists the hash the moment it becomes computable, never after execution.
"""


def no_op_compensate(context: PipelineContext, result: StepResult) -> None:
    del context, result


def no_op_persist(context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork) -> None:
    del context, result, uow


@dataclass(frozen=True, slots=True)
class PipelineStepDefinition:
    """One step's code-level identity, contract, and input-derivation rule.

    ``definition_id`` is the step's stable definition/version identity
    (ADR-0009 SS 6): a change here participates in the input hash even for
    otherwise-identical semantic input, so a registry change that alters a
    step's behavior is detectable as drift without a new
    ``pipeline_version``/``step_version`` column.
    """

    name: str
    definition_id: str
    step: PipelineStep
    input_deriver: InputDeriver

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("PipelineStepDefinition.name must not be empty")
        if not self.definition_id:
            raise ValueError("PipelineStepDefinition.definition_id must not be empty")

    def compute_input_hash(
        self,
        *,
        run_input: Mapping[str, Any],
        step_outputs: Mapping[str, Mapping[str, Any]],
    ) -> str | None:
        semantic_input = self.input_deriver(run_input, step_outputs)
        if semantic_input is None:
            return None
        return canonical_step_input_hash(
            definition_id=self.definition_id,
            semantic_input=semantic_input,
        )


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    """One pipeline type's ordered, immutable step sequence."""

    pipeline_type: PipelineType
    steps: tuple[PipelineStepDefinition, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError(
                f"PipelineDefinition for {self.pipeline_type!s} must have at least one step"
            )
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(
                f"PipelineDefinition for {self.pipeline_type!s} has duplicate step names"
            )


@dataclass(frozen=True, slots=True)
class StepPlan:
    """One step to materialize alongside a new ``PipelineRun`` (ADR-0009 SS
    B/SS C): stable name, ordinal, and an ``input_hash`` when already
    computable at submission time -- ``None`` otherwise, to be computed and
    persisted once the dependency it needs becomes available (SS 12)."""

    name: str
    ordinal: int
    input_hash: str | None = None


class UnknownPipelineTypeError(Exception):
    """No :class:`PipelineDefinition` is registered for this ``PipelineType``."""

    def __init__(self, pipeline_type: PipelineType) -> None:
        self.pipeline_type = pipeline_type
        super().__init__(f"No PipelineDefinition registered for {pipeline_type!s}")


class PipelineRegistry:
    """Closed, immutable ``PipelineType -> PipelineDefinition`` lookup.

    Built once from a fixed collection at construction time. There is no
    ``register()``/mutation method after that -- a registry that needs a
    different set of pipelines is a different :class:`PipelineRegistry`
    instance, never a mutated one.
    """

    def __init__(self, definitions: Sequence[PipelineDefinition]) -> None:
        by_type: dict[PipelineType, PipelineDefinition] = {}
        for definition in definitions:
            if definition.pipeline_type in by_type:
                raise ValueError(f"Duplicate PipelineDefinition for {definition.pipeline_type!s}")
            by_type[definition.pipeline_type] = definition
        self._definitions: Mapping[PipelineType, PipelineDefinition] = by_type

    def get(self, pipeline_type: PipelineType) -> PipelineDefinition:
        try:
            return self._definitions[pipeline_type]
        except KeyError:
            raise UnknownPipelineTypeError(pipeline_type) from None

    def build_step_plan(
        self,
        pipeline_type: PipelineType,
        *,
        run_input: Mapping[str, Any],
    ) -> list[StepPlan]:
        """The step plan a durable submission (SM-502's
        ``create_run_with_steps``) should persist for a brand-new run: every
        step in registry order, with ``input_hash`` populated for any step
        whose input is derivable purely from ``run_input`` (no prior step
        output exists yet at submission time)."""

        definition = self.get(pipeline_type)
        return [
            StepPlan(
                name=step_def.name,
                ordinal=ordinal,
                input_hash=step_def.compute_input_hash(run_input=run_input, step_outputs={}),
            )
            for ordinal, step_def in enumerate(definition.steps)
        ]


__all__ = [
    "InputDeriver",
    "PipelineDefinition",
    "PipelineRegistry",
    "PipelineStep",
    "PipelineStepDefinition",
    "StepPlan",
    "StepResult",
    "UnknownPipelineTypeError",
    "no_op_compensate",
    "no_op_persist",
]
