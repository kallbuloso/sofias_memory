"""Automatic-retry attempt ceiling and backoff/jitter policy (ADR-0009 SS K).

ADR-0009 deliberately left the exact ceiling/backoff constants as an SM-504
implementation decision (SS K: "the exact number is an implementation
constant set in SM-504, not re-opened by this ADR beyond requiring it
exists, is finite, and is the same concept driving next_attempt_at
scheduling"). These values are that decision.

Retry never sleeps in-process: computing a delay only produces a
``next_attempt_at`` timestamp to persist (ADR-0009 SS K/SS 23) -- the run's
current execution then ends, and a future claim (SM-503) picks it back up
once due.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

MAX_RUN_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
BACKOFF_JITTER_MAX_SECONDS = 1.0

type JitterSource = Callable[[], float]
"""Returns a jitter value in ``[0, BACKOFF_JITTER_MAX_SECONDS)``. Injectable
so tests can assert an exact, deterministic delay instead of a random one."""


def default_jitter_source() -> float:
    return random.uniform(0.0, BACKOFF_JITTER_MAX_SECONDS)  # noqa: S311 - not security-sensitive


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Pure backoff/ceiling policy: no I/O, no clock access of its own.

    Callers always supply ``run_attempt`` (``PipelineRun.attempt``, ADR-0009
    SS L) and add the returned delay to PostgreSQL's own ``now()`` -- this
    policy never reads a clock itself, keeping PostgreSQL the sole time
    authority (ADR-0009 SS 27).
    """

    max_run_attempts: int = MAX_RUN_ATTEMPTS
    backoff_base_seconds: float = BACKOFF_BASE_SECONDS
    backoff_cap_seconds: float = BACKOFF_CAP_SECONDS
    jitter_source: Callable[[], float] = default_jitter_source

    def has_attempts_remaining(self, run_attempt: int) -> bool:
        return run_attempt < self.max_run_attempts

    def backoff_seconds(self, run_attempt: int) -> float:
        """``base * 2^(attempt-1)``, capped, plus jitter (ADR-0009 SS 23)."""

        exponent = max(run_attempt - 1, 0)
        base_delay: float = min(self.backoff_cap_seconds, self.backoff_base_seconds * (2**exponent))
        source: Callable[[], float] = self.jitter_source
        jitter: float = source()
        result: float = base_delay + jitter
        return result
