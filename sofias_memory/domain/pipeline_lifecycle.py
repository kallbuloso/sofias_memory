"""Pure PipelineRun/PipelineStep state-machine contract, per ADR-0009.

This module has no dependency on FastAPI, SQLAlchemy, or Neo4j. It only knows
about the domain enums and decides whether a status transition is allowed.
Applying an allowed transition to a persisted row (timestamps, worker_id,
error fields, etc.) is a services-layer concern.
"""

from __future__ import annotations

from sofias_memory.domain.enums import PipelineRunStatus, PipelineStepStatus


class PipelineTransitionError(Exception):
    """Base class for an invalid persisted-pipeline state transition."""


class PipelineRunTransitionError(PipelineTransitionError):
    """A PipelineRun transition is not allowed by ADR-0009's state machine."""

    def __init__(self, current: PipelineRunStatus, target: PipelineRunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"PipelineRun cannot transition from {current!s} to {target!s}.")


class PipelineStepTransitionError(PipelineTransitionError):
    """A PipelineStep transition is not allowed by ADR-0009's state machine."""

    def __init__(self, current: PipelineStepStatus, target: PipelineStepStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"PipelineStep cannot transition from {current!s} to {target!s}.")


class PipelineProgressOutOfBoundsError(PipelineTransitionError):
    """PipelineRun.progress must stay within [0.0, 1.0]."""

    def __init__(self, value: float) -> None:
        self.value = value
        super().__init__(f"PipelineRun.progress must be within [0.0, 1.0], got {value!r}.")


# ADR-0009 SS A: PipelineRun transition matrix.
#
# Terminal states (SUCCEEDED, FAILED, CANCELLED) have no outgoing transitions:
# they are immutable. `failed` and `cancelled` never reopen the same run --
# only manual retry (SM-514) creates a *new* PipelineRun linked via
# `retry_of_run_id`. `queued -> running` is reserved for the future queue
# claim (SM-503). `running -> queued` exists only for durable automatic
# retry (SM-503/SM-504/SM-507), never as a generic "go back to queued" knob.
RUN_TRANSITIONS: frozenset[tuple[PipelineRunStatus, PipelineRunStatus]] = frozenset(
    {
        (PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING),
        (PipelineRunStatus.QUEUED, PipelineRunStatus.CANCELLED),
        (PipelineRunStatus.RUNNING, PipelineRunStatus.SUCCEEDED),
        (PipelineRunStatus.RUNNING, PipelineRunStatus.QUEUED),
        (PipelineRunStatus.RUNNING, PipelineRunStatus.FAILED),
        (PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING),
        (PipelineRunStatus.CANCELLING, PipelineRunStatus.CANCELLED),
        (PipelineRunStatus.CANCELLING, PipelineRunStatus.FAILED),
    }
)

# ADR-0009 SS B: PipelineStep transition matrix (the complete, unambiguous
# version -- an earlier draft of the ADR let SS B's summary and SS I's
# recovery rules disagree; this is the corrected single source).
#
# `queued -> cancelled`: the owning run was cancelled before this step ever
# started (SM-514).
# `running -> queued`: only a retryable error with attempts remaining (SS K),
# or RUNNING-stale recovery reconciling an abandoned claim (SS I) -- never
# executed inline by recovery itself.
# `running -> cancelled`: only CANCELLING-stale recovery classifying the
# orphaned step as case A or B, safe/reconciled (SS I).
#
# `PipelineStepStatus.CANCELLING` is intentionally absent from this matrix.
# The enum value is preserved for schema compatibility with
# `PipelineRunStatus`, but the normal B5 engine never routes a step through
# it: cancellation is observed only between steps (SS N), so the actively
# executing step simply runs to its own succeeded/failed outcome, and a
# queued-but-never-started step goes directly queued -> cancelled.
STEP_TRANSITIONS: frozenset[tuple[PipelineStepStatus, PipelineStepStatus]] = frozenset(
    {
        (PipelineStepStatus.QUEUED, PipelineStepStatus.RUNNING),
        (PipelineStepStatus.QUEUED, PipelineStepStatus.CANCELLED),
        (PipelineStepStatus.RUNNING, PipelineStepStatus.SUCCEEDED),
        (PipelineStepStatus.RUNNING, PipelineStepStatus.FAILED),
        (PipelineStepStatus.RUNNING, PipelineStepStatus.QUEUED),
        (PipelineStepStatus.RUNNING, PipelineStepStatus.CANCELLED),
    }
)


def validate_run_transition(current: PipelineRunStatus, target: PipelineRunStatus) -> None:
    """Raise ``PipelineRunTransitionError`` unless ADR-0009 allows this move."""

    if (current, target) not in RUN_TRANSITIONS:
        raise PipelineRunTransitionError(current, target)


def validate_step_transition(current: PipelineStepStatus, target: PipelineStepStatus) -> None:
    """Raise ``PipelineStepTransitionError`` unless ADR-0009 allows this move."""

    if (current, target) not in STEP_TRANSITIONS:
        raise PipelineStepTransitionError(current, target)


def validate_progress(value: float) -> None:
    """Raise ``PipelineProgressOutOfBoundsError`` unless ``value`` is in [0.0, 1.0]."""

    if value < 0.0 or value > 1.0:
        raise PipelineProgressOutOfBoundsError(value)
