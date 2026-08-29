"""Reusable OpenAPI response-documentation fragments (SWAGGER-002).

Every fragment here documents an error genuinely producible by the runtime,
confirmed against the service code that raises it (not guessed, and not
copied from docs/api.md without verification). All of them reference the
real ``ErrorEnvelope`` schema -- never a fabricated shape. Import the shared
constants where the same error applies to multiple operations; write a
one-off inline dict (via :func:`error_response`) for anything specific to a
single operation.
"""

from __future__ import annotations

from typing import Any

_ERROR_ENVELOPE_REF = "#/components/schemas/ErrorEnvelope"


def error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": _ERROR_ENVELOPE_REF}}},
    }


# Shared across every write that accepts Idempotency-Key and creates a new
# PipelineRun (Remember text/file/url, Cognify, Improve, Forget) -- confirmed
# in services/pipeline_submission.py.
RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400 = error_response(
    "The Idempotency-Key uses the internally reserved 'sys:' namespace. "
    "ErrorEnvelope with error.code=RESERVED_IDEMPOTENCY_KEY_NAMESPACE."
)
WORKER_DISABLED_503 = error_response(
    "The internal worker is not operational; a new PipelineRun cannot be "
    "created right now. ErrorEnvelope with error.code=WORKER_DISABLED."
)

DATASET_NOT_FOUND_404 = error_response(
    "The target dataset does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)

# The single 409 response for a write that accepts Idempotency-Key AND
# targets a dataset (Remember, Cognify, Improve, Forget with source/dataset
# scope): OpenAPI allows only one response object per status code, so both
# genuinely possible conflict codes are documented together.
IDEMPOTENCY_OR_DATASET_CONFLICT_409 = error_response(
    "Conflict with the requested durable operation. ErrorEnvelope with "
    "error.code one of: IDEMPOTENCY_CONFLICT (the same Idempotency-Key was "
    "already used for different work), DATASET_DELETING, or DATASET_DELETED "
    "(the target dataset has an in-flight or completed administrative delete)."
)

# GET /api/v1/runs/{run_id}/retry: no client-supplied Idempotency-Key (the
# server derives its own), but retry can still be rejected because the
# original run isn't retryable, or because its dataset is administratively
# blocked -- confirmed in services/run_control.py.
RUN_RETRY_CONFLICT_409 = error_response(
    "Conflict with retrying this run. ErrorEnvelope with error.code one of: "
    "RUN_NOT_RETRYABLE (the run is not in a retryable terminal state), "
    "DATASET_DELETING, or DATASET_DELETED (the run's dataset has an "
    "in-flight or completed administrative delete)."
)

RUN_NOT_FOUND_404 = error_response(
    "The pipeline run does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
