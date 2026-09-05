"""Domain-level contracts for Sofias Memory."""

from sofias_memory.domain.enums import (
    DatasetStatus,
    GraphOutboxOperation,
    GraphOutboxStatus,
    MemoryEntryType,
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
    SessionStatus,
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
from sofias_memory.domain.session_id import (
    SESSION_ID_MAX_LENGTH,
    InvalidSessionIdError,
    normalize_session_id,
)

__all__ = [
    "RUN_TRANSITIONS",
    "SESSION_ID_MAX_LENGTH",
    "STEP_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "DatasetStatus",
    "GraphOutboxOperation",
    "GraphOutboxStatus",
    "InvalidSessionIdError",
    "MemoryEntryType",
    "PipelineProgressOutOfBoundsError",
    "PipelineRunStatus",
    "PipelineRunTransitionError",
    "PipelineStepStatus",
    "PipelineStepTransitionError",
    "PipelineTransitionError",
    "PipelineType",
    "SessionStatus",
    "SourceKind",
    "SourceStatus",
    "SummaryTargetType",
    "normalize_session_id",
    "validate_progress",
    "validate_run_transition",
    "validate_step_transition",
]
