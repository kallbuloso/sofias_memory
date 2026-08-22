"""Typed step-execution error contract (ADR-0009 SS 21/22/X).

A step implementation decides explicitly whether a failure is retryable or
permanent by raising one of these two types. The engine never classifies an
error by substring/message inspection. Only a stable ``code`` and a safe,
public-compatible ``message`` are ever persisted -- never a traceback, raw
provider payload, or internal path.
"""

from __future__ import annotations


class PipelineStepError(Exception):
    """Base class for a typed, classified step-execution failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RetryablePipelineStepError(PipelineStepError):
    """Transient dependency/infrastructure failure (ADR-0009 SS K/SS X).

    The engine requeues the run with backoff when attempts remain, or fails
    it once the run's attempt ceiling is exhausted.
    """


class PermanentPipelineStepError(PipelineStepError):
    """Non-retryable failure (ADR-0009 SS X): the run fails immediately, no
    retry is scheduled."""


STEP_INPUT_DRIFT_ERROR_CODE = "STEP_INPUT_DRIFT"
"""Persisted plan vs. current registry mismatch (ADR-0009 SS B/SS 14)."""

STEP_INPUT_UNRESOLVABLE_ERROR_CODE = "STEP_INPUT_UNRESOLVABLE"
"""A step about to execute could not derive its input even though every
step ordered before it already succeeded -- a registry/definition bug, not a
transient condition. Distinct from STEP_INPUT_DRIFT (which means "the
persisted plan disagrees with the registry"), this means "the registry
itself cannot resolve this step's input at the only point it is ever
allowed to be resolved"."""

UNEXPECTED_STEP_ERROR_CODE = "STEP_UNEXPECTED_ERROR"
"""Fail-safe classification for an untyped/unexpected exception raised by a
step (ADR-0009 SS 22): never retried automatically, sanitized message only."""

CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE = "CONFIG_FINGERPRINT_MISMATCH"
"""Stale RUNNING recovery found a different config_fingerprint than the
current process (ADR-0009 SS I/SS J, SM-507). Never resumed."""

WORKER_LOST_ERROR_CODE = "WORKER_LOST"
"""Stale recovery could not safely resume this run: attempts exhausted, no
durable step plan exists (legacy pre-B5 run), or persisted state violates an
engine invariant (ADR-0009 SS I/SS X, SM-507)."""

CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE = "CANCEL_RECOVERY_AMBIGUOUS"
"""Stale CANCELLING recovery could not prove the orphaned step's effect was
either uncommitted or safely reconciled (ADR-0009 SS I case C, SM-507). The
run fails rather than falsely reporting CANCELLED; manual retry is the path
forward."""
