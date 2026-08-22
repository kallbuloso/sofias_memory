"""Domain-level contracts for Sofias Memory."""

from sofias_memory.domain.enums import (
    DatasetStatus,
    GraphOutboxOperation,
    GraphOutboxStatus,
    MemoryEntryType,
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
    SummaryTargetType,
)
from sofias_memory.domain.pipeline_lifecycle import (
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    PipelineProgressOutOfBoundsError,
    PipelineRunTransitionError,
    PipelineStepTransitionError,
    PipelineTransitionError,
    validate_progress,
    validate_run_transition,
    validate_step_transition,
)

__all__ = [
    "RUN_TRANSITIONS",
    "STEP_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "DatasetStatus",
    "GraphOutboxOperation",
    "GraphOutboxStatus",
    "MemoryEntryType",
    "PipelineProgressOutOfBoundsError",
    "PipelineRunStatus",
    "PipelineRunTransitionError",
    "PipelineStepStatus",
    "PipelineStepTransitionError",
    "PipelineTransitionError",
    "PipelineType",
    "SourceKind",
    "SourceStatus",
    "SummaryTargetType",
    "validate_progress",
    "validate_run_transition",
    "validate_step_transition",
]
