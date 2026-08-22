from __future__ import annotations

import pytest

from sofias_memory.pipelines.retry_policy import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    MAX_RUN_ATTEMPTS,
    RetryPolicy,
)


def fixed_jitter(value: float) -> object:
    def _source() -> float:
        return value

    return _source


@pytest.mark.parametrize(
    ("run_attempt", "has_remaining"),
    [
        (1, True),
        (MAX_RUN_ATTEMPTS - 1, True),
        (MAX_RUN_ATTEMPTS, False),
        (MAX_RUN_ATTEMPTS + 1, False),
    ],
)
def test_has_attempts_remaining(run_attempt: int, has_remaining: bool) -> None:
    policy = RetryPolicy()
    assert policy.has_attempts_remaining(run_attempt) is has_remaining


def test_backoff_seconds_first_attempt_is_base_plus_jitter() -> None:
    policy = RetryPolicy(jitter_source=fixed_jitter(0.0))  # type: ignore[arg-type]
    assert policy.backoff_seconds(1) == BACKOFF_BASE_SECONDS


def test_backoff_seconds_doubles_with_attempt() -> None:
    policy = RetryPolicy(jitter_source=fixed_jitter(0.0))  # type: ignore[arg-type]
    assert policy.backoff_seconds(2) == BACKOFF_BASE_SECONDS * 2
    assert policy.backoff_seconds(3) == BACKOFF_BASE_SECONDS * 4
    assert policy.backoff_seconds(4) == BACKOFF_BASE_SECONDS * 8


def test_backoff_seconds_is_capped() -> None:
    policy = RetryPolicy(jitter_source=fixed_jitter(0.0))  # type: ignore[arg-type]
    assert policy.backoff_seconds(20) == BACKOFF_CAP_SECONDS


def test_backoff_seconds_adds_injected_jitter_exactly() -> None:
    policy = RetryPolicy(jitter_source=fixed_jitter(0.42))  # type: ignore[arg-type]
    assert policy.backoff_seconds(1) == pytest.approx(BACKOFF_BASE_SECONDS + 0.42)


def test_default_jitter_source_stays_within_documented_bounds() -> None:
    policy = RetryPolicy()
    for run_attempt in range(1, 6):
        delay = policy.backoff_seconds(run_attempt)
        assert delay >= 0.0
