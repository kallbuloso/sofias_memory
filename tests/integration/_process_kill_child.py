"""Child-process entrypoint for the GATE-B5 SS23/SS24 real OS process-kill
recovery test (``test_process_kill_recovery_postgres_integration.py``).

Runs the actual production ``sofias_memory.lifespan.lifespan`` context
manager -- the same startup/shutdown sequence the real FastAPI app uses --
against a real PostgreSQL database, with Neo4j disabled (out of scope for
this specific queue/worker/recovery proof) and a registry where REMEMBER's
one step is a deterministic test double that blocks forever once claimed,
so the parent test controls exactly when/where this process gets killed.
Every other pipeline type keeps its real, production step sequence.

Not a pytest test module (no ``test_`` prefix): invoked as a real OS
subprocess by the parent test, never collected or imported by pytest
itself.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402

from sofias_memory.config import load_settings  # noqa: E402
from sofias_memory.domain import PipelineType  # noqa: E402
from sofias_memory.lifespan import lifespan  # noqa: E402
from sofias_memory.pipelines.context import PipelineContext  # noqa: E402
from sofias_memory.pipelines.registry import (  # noqa: E402
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
    build_default_pipeline_registry,
)


class HangingStep:
    """Claims durably, then blocks forever -- the crash point the parent
    test kills this process at. Never reaches ``persist()``."""

    async def execute(self, context: PipelineContext) -> StepResult:
        print(f"CHILD_STEP_CLAIMED run_id={context.run_id}", flush=True)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        raise AssertionError("HangingStep never reaches persist()")

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        return None


def hanging_input_deriver(
    run_input: Mapping[str, Any], step_outputs: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    del step_outputs
    return {"seed": str(run_input.get("marker", "hang"))}


def build_test_registry() -> PipelineRegistry:
    default_registry = build_default_pipeline_registry()
    hanging_definition = PipelineDefinition(
        pipeline_type=PipelineType.REMEMBER,
        steps=(
            PipelineStepDefinition(
                name="hang",
                definition_id="process_kill_test_hang:v1",
                step=HangingStep(),
                input_deriver=hanging_input_deriver,
            ),
        ),
    )
    other_definitions = [
        default_registry.get(pipeline_type)
        for pipeline_type in (
            PipelineType.COGNIFY,
            PipelineType.IMPROVE,
            PipelineType.FORGET,
            PipelineType.DATASET_DELETE,
        )
    ]
    return PipelineRegistry([hanging_definition, *other_definitions])


async def main() -> None:
    settings = load_settings()
    app = FastAPI(title="process-kill-test", lifespan=lifespan)
    app.state.settings = settings

    from sofias_memory.infrastructure.postgres import (
        create_async_engine_from_settings,
        create_session_factory,
    )
    from sofias_memory.services.pipeline_recovery import PipelineRecoveryService
    from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

    engine = create_async_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    app.state.postgres_session_factory = session_factory
    app.state.postgres_engine = engine

    registry = build_test_registry()
    app.state.pipeline_registry = registry

    worker = PipelineWorkerCoordinator(
        session_factory,
        registry,
        enabled=True,
        poll_interval_ms=settings.worker_poll_interval_ms,
        stale_after_seconds=settings.worker_stale_after_seconds,
        max_concurrent_datasets=settings.worker_max_concurrent_datasets,
    )
    app.state.pipeline_worker = worker
    app.state.pipeline_recovery = PipelineRecoveryService(
        session_factory,
        registry,
        stale_after_seconds=settings.worker_stale_after_seconds,
        config_fingerprint=settings.config_fingerprint(),
    )
    app.state.readiness_checks = ()

    print(f"CHILD_WORKER_ID={worker.worker_id}", flush=True)

    async with lifespan(app):
        print("CHILD_LIFESPAN_STARTED", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
