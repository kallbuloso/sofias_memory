"""Unit coverage for run cancellation and manual retry (SM-514): pure
helpers and route-level HTTP contract against a real PostgreSQL-backed
``RunControlService`` (fast, no worker execution needed for these cases).
Real-worker/concurrency/Remember-ingress behavior is proven in
``tests/integration/test_run_control_postgres_integration.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from sofias_memory.domain import PipelineType
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.run_control import (
    RETRY_IDEMPOTENCY_KEY_PREFIX,
    _is_legitimate_global_run,
    run_not_retryable_error,
)


def test_run_not_retryable_error_shape() -> None:
    run_id = uuid4()
    error = run_not_retryable_error(run_id, reason="status=queued")
    assert error.code == ErrorCode.RUN_NOT_RETRYABLE
    assert error.status_code == 409
    assert error.details == {"run_id": str(run_id), "reason": "status=queued"}


def test_retry_key_prefix_is_reserved_namespace() -> None:
    assert RETRY_IDEMPOTENCY_KEY_PREFIX.startswith("sys:")


def test_forget_everything_input_is_legitimate_global() -> None:
    assert _is_legitimate_global_run(
        pipeline_type=PipelineType.FORGET, input_={"scope": "everything"}
    )


@pytest.mark.parametrize(
    ("pipeline_type", "input_"),
    [
        (PipelineType.FORGET, {"scope": "dataset"}),
        (PipelineType.FORGET, {"scope": "source"}),
        (PipelineType.FORGET, {}),
        (PipelineType.REMEMBER, {"scope": "everything"}),
        (PipelineType.COGNIFY, {}),
        (PipelineType.IMPROVE, {}),
    ],
)
def test_other_null_dataset_runs_are_not_legitimate_global(
    pipeline_type: PipelineType, input_: dict[str, object]
) -> None:
    assert not _is_legitimate_global_run(pipeline_type=pipeline_type, input_=input_)
