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
from sofias_memory.domain.session_context import (
    SESSION_CONTEXT_BLOCK_SEPARATOR,
    SessionContextCandidate,
    render_session_context_block,
    render_session_context_entry,
    select_session_context,
)
from sofias_memory.domain.session_entry_external_id import (
    SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH,
    InvalidSessionEntryExternalIdError,
    normalize_session_entry_external_id,
)
from sofias_memory.domain.session_id import (
    SESSION_ID_MAX_LENGTH,
    InvalidSessionIdError,
    normalize_session_id,
)

__all__ = [
    "RUN_TRANSITIONS",
    "SESSION_CONTEXT_BLOCK_SEPARATOR",
    "SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH",
    "SESSION_ID_MAX_LENGTH",
    "STEP_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "DatasetStatus",
    "GraphOutboxOperation",
    "GraphOutboxStatus",
    "InvalidSessionEntryExternalIdError",
    "InvalidSessionIdError",
    "MemoryEntryType",
    "PipelineProgressOutOfBoundsError",
    "PipelineRunStatus",
    "PipelineRunTransitionError",
    "PipelineStepStatus",
    "PipelineStepTransitionError",
    "PipelineTransitionError",
    "PipelineType",
    "SessionContextCandidate",
    "SessionStatus",
    "SourceKind",
    "SourceStatus",
    "SummaryTargetType",
    "normalize_session_entry_external_id",
    "normalize_session_id",
    "render_session_context_block",
    "render_session_context_entry",
    "select_session_context",
    "validate_progress",
    "validate_run_transition",
    "validate_step_transition",
]
