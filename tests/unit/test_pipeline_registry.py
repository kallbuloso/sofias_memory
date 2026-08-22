from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sofias_memory.domain import PipelineType
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.registry import (
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
    UnknownPipelineTypeError,
    no_op_compensate,
)


class FakeStep:
    async def execute(self, context: PipelineContext) -> StepResult:  # pragma: no cover - unused
        del context
        return StepResult()

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def run_input_deriver(
    run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]
) -> Mapping[str, Any]:
    del step_outputs
    return {"value": run_input.get("value")}


def no_hash_deriver(run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]) -> None:
    del run_input, step_outputs
    return None


def make_step_def(name: str, definition_id: str = "step:v1") -> PipelineStepDefinition:
    return PipelineStepDefinition(
        name=name,
        definition_id=definition_id,
        step=FakeStep(),
        input_deriver=run_input_deriver,
    )


# --- PipelineStepDefinition -------------------------------------------------


def test_step_definition_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        PipelineStepDefinition(
            name="", definition_id="x:v1", step=FakeStep(), input_deriver=run_input_deriver
        )


def test_step_definition_rejects_empty_definition_id() -> None:
    with pytest.raises(ValueError, match="definition_id"):
        PipelineStepDefinition(
            name="a", definition_id="", step=FakeStep(), input_deriver=run_input_deriver
        )


def test_step_definition_compute_input_hash_none_when_deriver_returns_none() -> None:
    step_def = PipelineStepDefinition(
        name="a", definition_id="x:v1", step=FakeStep(), input_deriver=no_hash_deriver
    )
    assert step_def.compute_input_hash(run_input={}, step_outputs={}) is None


def test_step_definition_compute_input_hash_deterministic() -> None:
    step_def = make_step_def("a")
    first = step_def.compute_input_hash(run_input={"value": 1}, step_outputs={})
    second = step_def.compute_input_hash(run_input={"value": 1}, step_outputs={})
    assert first == second
    assert first is not None


# --- PipelineDefinition ------------------------------------------------------


def test_pipeline_definition_rejects_empty_steps() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        PipelineDefinition(pipeline_type=PipelineType.COGNIFY, steps=())


def test_pipeline_definition_rejects_duplicate_step_names() -> None:
    with pytest.raises(ValueError, match="duplicate step names"):
        PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(make_step_def("a"), make_step_def("a")),
        )


def test_pipeline_definition_accepts_ordered_unique_steps() -> None:
    definition = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY,
        steps=(make_step_def("a"), make_step_def("b")),
    )
    assert [s.name for s in definition.steps] == ["a", "b"]


# --- PipelineRegistry --------------------------------------------------------


def test_registry_lookup_known_pipeline_type() -> None:
    definition = PipelineDefinition(pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("a"),))
    registry = PipelineRegistry([definition])
    assert registry.get(PipelineType.COGNIFY) is definition


def test_registry_lookup_unknown_pipeline_type_raises() -> None:
    registry = PipelineRegistry([])
    with pytest.raises(UnknownPipelineTypeError):
        registry.get(PipelineType.REMEMBER)


def test_registry_rejects_duplicate_pipeline_type() -> None:
    definition_a = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("a"),)
    )
    definition_b = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("b"),)
    )
    with pytest.raises(ValueError, match="Duplicate PipelineDefinition"):
        PipelineRegistry([definition_a, definition_b])


def test_registry_has_no_mutation_method() -> None:
    """Closed registry (ADR-0009 SS O): no register()/add() method exists
    to mutate it after construction."""

    registry = PipelineRegistry([])
    assert not hasattr(registry, "register")
    assert not hasattr(registry, "add")


# --- build_step_plan ----------------------------------------------------------


def test_build_step_plan_orders_and_names_match_registry() -> None:
    definition = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY,
        steps=(make_step_def("a"), make_step_def("b"), make_step_def("c")),
    )
    registry = PipelineRegistry([definition])
    plan = registry.build_step_plan(PipelineType.COGNIFY, run_input={"value": 1})
    assert [(p.name, p.ordinal) for p in plan] == [("a", 0), ("b", 1), ("c", 2)]


def test_build_step_plan_populates_hash_computable_from_run_input_alone() -> None:
    definition = PipelineDefinition(pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("a"),))
    registry = PipelineRegistry([definition])
    plan = registry.build_step_plan(PipelineType.COGNIFY, run_input={"value": 1})
    assert plan[0].input_hash is not None
    assert len(plan[0].input_hash) == 64


def test_build_step_plan_leaves_dependent_hash_null() -> None:
    dependent_step = PipelineStepDefinition(
        name="b", definition_id="b:v1", step=FakeStep(), input_deriver=no_hash_deriver
    )
    definition = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("a"), dependent_step)
    )
    registry = PipelineRegistry([definition])
    plan = registry.build_step_plan(PipelineType.COGNIFY, run_input={"value": 1})
    assert plan[0].input_hash is not None
    assert plan[1].input_hash is None


def test_build_step_plan_matches_the_engine_visible_definition() -> None:
    """The same registry that the engine validates against also produces
    the submission-time StepPlan -- there is only one source of truth for
    "what steps this pipeline has" (SM-504 story requirement)."""

    definition = PipelineDefinition(
        pipeline_type=PipelineType.COGNIFY, steps=(make_step_def("a"), make_step_def("b"))
    )
    registry = PipelineRegistry([definition])
    plan = registry.build_step_plan(PipelineType.COGNIFY, run_input={"value": 1})
    engine_steps = registry.get(PipelineType.COGNIFY).steps
    assert [p.name for p in plan] == [s.name for s in engine_steps]
