from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from sofias_memory.observability.logging import get_logger

READINESS_CHECK_TIMEOUT_SECONDS = 1.0
SAFE_EXCEPTION_DETAIL = "check failed"
TIMEOUT_DETAIL = "check timed out"

ReadinessStatus = Literal["ready", "not_ready"]
LivenessStatus = Literal["ok"]
ReadinessCheck = Callable[[], Awaitable["ReadinessCheckResult"]]
ReadinessCheckRegistry = Mapping[str, ReadinessCheck] | Iterable[tuple[str, ReadinessCheck]]

router = APIRouter(tags=["health"])


class LiveHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LivenessStatus = "ok"


class ReadinessCheckStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    detail: str | None = Field(default=None, min_length=1)


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReadinessStatus
    checks: dict[str, ReadinessCheckStatus]


@dataclass(frozen=True)
class ReadinessCheckResult:
    ready: bool
    detail: str | None = None


@dataclass(frozen=True)
class RegisteredReadinessCheck:
    name: str
    check: ReadinessCheck


@router.get(
    "/health/live",
    response_model=LiveHealthResponse,
    summary="Check process liveness",
    description=(
        "Reports whether the process is running at all. Never checks PostgreSQL, "
        "Neo4j, or the worker -- a process that answers here can still have "
        "`/health/ready` report `not_ready`. Never requires `X-API-Key`."
    ),
)
async def live() -> LiveHealthResponse:
    return LiveHealthResponse()


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    summary="Check dependency and worker readiness",
    description=(
        "Reports whether PostgreSQL, Neo4j, and the internal worker are all "
        "reachable and operational. Returns `503` with `status=not_ready` if any "
        "check fails -- existing reads may still work, but a request that would "
        "create a new durable PipelineRun can be rejected. Never requires "
        "`X-API-Key`."
    ),
)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    checks = app_readiness_checks(request.app)
    readiness = await run_readiness_checks(checks)
    if readiness.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return readiness


def app_readiness_checks(app: object) -> tuple[RegisteredReadinessCheck, ...]:
    state = getattr(app, "state", None)
    checks = getattr(state, "readiness_checks", ())
    if isinstance(checks, tuple):
        return checks
    return ()


def validate_readiness_checks(
    readiness_checks: ReadinessCheckRegistry = (),
) -> tuple[RegisteredReadinessCheck, ...]:
    check_items = (
        readiness_checks.items() if isinstance(readiness_checks, Mapping) else readiness_checks
    )
    registered_checks: list[RegisteredReadinessCheck] = []
    seen_names: set[str] = set()
    for name, check in check_items:
        if not name or name.isspace():
            raise ValueError("readiness check name must not be empty")
        if name in seen_names:
            raise ValueError(f"duplicate readiness check name: {name}")
        seen_names.add(name)
        registered_checks.append(RegisteredReadinessCheck(name=name, check=check))
    return tuple(registered_checks)


async def run_readiness_checks(
    checks: Iterable[RegisteredReadinessCheck],
    timeout_seconds: float = READINESS_CHECK_TIMEOUT_SECONDS,
) -> ReadinessResponse:
    ordered_checks = tuple(checks)
    if not ordered_checks:
        return ReadinessResponse(status="ready", checks={})

    results = await asyncio.gather(
        *(
            _run_single_check(registered_check, timeout_seconds=timeout_seconds)
            for registered_check in ordered_checks
        )
    )
    ordered_results = sorted(results, key=lambda result: result[0])
    check_statuses = {
        name: ReadinessCheckStatus(ready=result.ready, detail=result.detail)
        for name, result in ordered_results
    }
    readiness_status: ReadinessStatus = (
        "ready" if all(result.ready for _, result in ordered_results) else "not_ready"
    )
    return ReadinessResponse(status=readiness_status, checks=check_statuses)


async def _run_single_check(
    registered_check: RegisteredReadinessCheck,
    timeout_seconds: float,
) -> tuple[str, ReadinessCheckResult]:
    try:
        result = await asyncio.wait_for(registered_check.check(), timeout=timeout_seconds)
        return registered_check.name, result
    except TimeoutError:
        result = ReadinessCheckResult(ready=False, detail=TIMEOUT_DETAIL)
        _log_failed_check(registered_check.name, "TimeoutError")
        return registered_check.name, result
    except Exception as exc:
        result = ReadinessCheckResult(ready=False, detail=SAFE_EXCEPTION_DETAIL)
        _log_failed_check(registered_check.name, type(exc).__name__)
        return registered_check.name, result


def _log_failed_check(name: str, exception_type: str) -> None:
    get_logger(__name__).warning(
        "readiness_check_failed",
        check=name,
        exception_type=exception_type,
    )
