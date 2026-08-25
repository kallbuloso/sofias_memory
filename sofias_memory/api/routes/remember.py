"""Remember routes (SM-513).

A pure submission/observation boundary, mirroring ``routes.cognify``/
``routes.improve``/``routes.forget`` (SM-510/511/512): it validates the
request, stages any bytes that must survive past the HTTP response into
durable ingress storage (SM-513 SS 9 -- TEXT/FILE only; URL fetch happens in
the worker), submits a durable ``PipelineRun`` through the shared SM-509
contract, and -- when the caller asked to wait -- observes the run's
persisted terminal state. It never executes a loader, writes final source
storage, or invokes Cognify itself; all of that is the worker's job
(``pipelines.steps.remember``).
"""

from __future__ import annotations

import json
from hashlib import sha256
from http import HTTPStatus
from pathlib import PurePosixPath
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from pydantic import TypeAdapter, ValidationError

from sofias_memory.api.errors import SofiasMemoryError, current_request_id
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.lifespan import (
    app_pipeline_registry,
    app_pipeline_worker,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.loaders.text import (
    SUPPORTED_TEXT_FILE_EXTENSIONS,
    TextFileLoadError,
    prepare_text_content,
    sanitize_upload_filename,
)
from sofias_memory.loaders.url import normalize_and_validate_https_url
from sofias_memory.schemas.common import ErrorCode, JSONValue, ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.remember import (
    RememberTextRequest,
    RememberTextResult,
    RememberUrlRequest,
)
from sofias_memory.services.pipeline_submission import (
    PipelineSubmissionService,
    PreparationHook,
    SubmissionOutcome,
    SubmissionTargets,
    SubmissionUnitOfWork,
)
from sofias_memory.services.pipeline_waiter import PipelineRunWaiter
from sofias_memory.services.remember import (
    DEFAULT_DATASET_SLUG,
    REMEMBER_RESULT_METRIC_KEY,
    dataset_not_found_error,
    delete_ingress_artifact,
    remember_file_run_input,
    remember_text_run_input,
    remember_url_run_input,
    same_remember_intent,
    validate_remember_mode,
    write_ingress_bytes,
)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
UPLOAD_READ_CHUNK_SIZE_BYTES = 1024 * 1024
METADATA_ADAPTER = TypeAdapter(dict[str, JSONValue])

router = APIRouter(tags=["remember"])


@router.post(
    "/remember",
    response_model=SuccessEnvelope[RememberTextResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        }
    },
)
async def remember_text(
    payload: RememberTextRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    validate_remember_mode(payload.mode)
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)

    # Pure, deterministic text encoding/hash -- acceptable at the boundary
    # because it is needed for the work identity itself (SM-513 SS 11), not
    # a duplicate of any format-specific loader.
    prepared_text = prepare_text_content(payload.content)
    work_input = remember_text_run_input(
        dataset=payload.dataset,
        content_sha256=prepared_text.content_sha256,
        name=payload.name,
        metadata=payload.metadata,
        session_id=payload.session_id,
        mode=payload.mode,
        force=payload.force,
    )

    candidate_run_id = uuid4()
    write_ingress_bytes(
        settings.data_directory,
        run_id=candidate_run_id,
        raw_bytes=prepared_text.original_bytes,
    )

    submission = _build_submission(request, settings, session_factory)
    outcome = await _submit_with_ingress_cleanup(
        submission,
        settings=settings,
        candidate_run_id=candidate_run_id,
        work_input=work_input,
        idempotency_key=idempotency_key,
        prepare=_dataset_preparation_hook(payload.dataset),
    )

    status = await _await_if_requested(
        session_factory, settings=settings, outcome=outcome, wait=payload.wait
    )
    return await _respond(response, session_factory=session_factory, outcome=outcome, status=status)


@router.post(
    "/remember/url",
    response_model=SuccessEnvelope[RememberTextResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        }
    },
)
async def remember_url(
    payload: RememberUrlRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    validate_remember_mode(payload.mode)
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)

    # Syntax/public-contract validation only (SM-513 SS 13) -- no network
    # I/O here. The actual fetch happens in the worker's own execute()
    # phase, using the identical SSRF-guarded fetch_https_url.
    normalized_url = normalize_and_validate_https_url(payload.url)

    work_input = remember_url_run_input(
        dataset=payload.dataset,
        url=normalized_url,
        metadata=payload.metadata,
        session_id=payload.session_id,
        mode=payload.mode,
        force=payload.force,
    )

    # Nothing is staged for URL at this point -- content is not known until
    # the worker fetches it -- so the candidate run id exists purely to give
    # the new run a deterministic identity; ingress cleanup below is a safe
    # no-op when nothing was ever written under it.
    candidate_run_id = uuid4()

    submission = _build_submission(request, settings, session_factory)
    outcome = await _submit_with_ingress_cleanup(
        submission,
        settings=settings,
        candidate_run_id=candidate_run_id,
        work_input=work_input,
        idempotency_key=idempotency_key,
        prepare=_dataset_preparation_hook(payload.dataset),
    )

    status = await _await_if_requested(
        session_factory, settings=settings, outcome=outcome, wait=payload.wait
    )
    return await _respond(response, session_factory=session_factory, outcome=outcome, status=status)


@router.post(
    "/remember/file",
    response_model=SuccessEnvelope[RememberTextResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        }
    },
)
async def remember_file(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    dataset: Annotated[str, Form()] = "main",
    metadata: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = None,
    mode: Annotated[str, Form()] = "ingest",
    wait: Annotated[bool, Form()] = True,
    force: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    validate_remember_mode(mode)
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)

    original_bytes = await read_upload_file_bytes(
        file, max_bytes=settings.max_source_size_mb * 1024 * 1024
    )
    parsed_metadata = parse_metadata_json(metadata)
    # Structural-only validation (filename/extension), never the actual
    # extraction algorithm -- that runs once, in the worker (SM-513 SS 12).
    filename = _validate_supported_extension(file.filename)
    content_sha256 = sha256(original_bytes).hexdigest()

    dataset_slug = dataset.strip() or "main"
    work_input = remember_file_run_input(
        dataset=dataset_slug,
        content_sha256=content_sha256,
        filename=filename,
        metadata=parsed_metadata,
        session_id=session_id.strip() if session_id else None,
        mode=mode,
        force=force,
    )

    candidate_run_id = uuid4()
    write_ingress_bytes(
        settings.data_directory,
        run_id=candidate_run_id,
        raw_bytes=original_bytes,
        filename=filename,
    )

    submission = _build_submission(request, settings, session_factory)
    outcome = await _submit_with_ingress_cleanup(
        submission,
        settings=settings,
        candidate_run_id=candidate_run_id,
        work_input=work_input,
        idempotency_key=idempotency_key,
        prepare=_dataset_preparation_hook(dataset_slug),
    )

    status = await _await_if_requested(
        session_factory, settings=settings, outcome=outcome, wait=wait
    )
    return await _respond(response, session_factory=session_factory, outcome=outcome, status=status)


def _build_submission(
    request: Request, settings: Settings, session_factory: AsyncSessionFactory
) -> PipelineSubmissionService:
    return PipelineSubmissionService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        config_fingerprint=settings.config_fingerprint(),
        session_factory=session_factory,
    )


async def _submit_with_ingress_cleanup(
    submission: PipelineSubmissionService,
    *,
    settings: Settings,
    candidate_run_id: UUID,
    work_input: dict[str, JSONValue],
    idempotency_key: str | None,
    prepare: PreparationHook,
) -> SubmissionOutcome:
    """Submit, then clean up this request's candidate ingress unless it is
    exactly the run that will execute it (SM-513 SS 9/10/38): a losing
    idempotency-key race, an existing-run replay, or any raised error (a
    validation failure inside ``prepare()``, or ``WORKER_DISABLED``) must
    never leave a permanent orphaned ingress directory behind."""

    try:
        outcome = await submission.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input=work_input,
            idempotency_key=idempotency_key,
            prepare=prepare,
            run_id=candidate_run_id,
            legacy_intent_equivalent=same_remember_intent,
        )
    except Exception:
        delete_ingress_artifact(settings.data_directory, run_id=candidate_run_id)
        raise
    if not outcome.created:
        delete_ingress_artifact(settings.data_directory, run_id=candidate_run_id)
    return outcome


async def _await_if_requested(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings,
    outcome: SubmissionOutcome,
    wait: bool,
) -> PipelineRunStatus:
    if not wait or outcome.terminal:
        return outcome.status
    waited = await PipelineRunWaiter(session_factory=session_factory).wait_for_terminal(
        outcome.run_id,
        timeout_seconds=settings.request_wait_timeout_seconds,
    )
    return waited.status


def _dataset_preparation_hook(dataset_slug: str) -> PreparationHook:
    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        postgres_uow = cast(PostgresUnitOfWork, uow)
        dataset = await postgres_uow.datasets.get_by_slug(dataset_slug)
        if dataset is None:
            if dataset_slug != DEFAULT_DATASET_SLUG:
                raise dataset_not_found_error(dataset_slug)
            # Lazy get-or-create, racing safely against a concurrent
            # first-ever caller (SM-513 SS 7): INSERT ... ON CONFLICT DO
            # NOTHING + re-read, never a bare get-then-add.
            dataset = await postgres_uow.datasets.get_or_create_by_slug(
                Dataset(
                    id=uuid4(),
                    name=DEFAULT_DATASET_SLUG,
                    slug=DEFAULT_DATASET_SLUG,
                    description=None,
                    status=DatasetStatus.ACTIVE,
                    active_generation=0,
                )
            )
        if dataset.status != DatasetStatus.ACTIVE:
            raise dataset_not_found_error(dataset_slug)
        return SubmissionTargets(dataset_id=dataset.id, source_id=None)

    return prepare


async def _respond(
    response: Response,
    *,
    session_factory: AsyncSessionFactory,
    outcome: SubmissionOutcome,
    status: PipelineRunStatus,
) -> SuccessEnvelope[RememberTextResult]:
    if status == PipelineRunStatus.SUCCEEDED:
        result = await _succeeded_result(session_factory, run_id=outcome.run_id)
    elif status == PipelineRunStatus.FAILED:
        raise await _failed_run_error(session_factory, run_id=outcome.run_id)
    else:
        result = RememberTextResult(run_id=outcome.run_id, status=status)
        if status != PipelineRunStatus.CANCELLED:
            response.status_code = HTTPStatus.ACCEPTED

    return SuccessEnvelope[RememberTextResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


async def _succeeded_result(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> RememberTextResult:
    metrics = await _run_metrics(session_factory, run_id=run_id)
    persisted = metrics.get(REMEMBER_RESULT_METRIC_KEY)
    if not isinstance(persisted, dict):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Remember run succeeded without a persisted result.",
            details={"run_id": str(run_id)},
        )
    return RememberTextResult(
        run_id=run_id,
        status=PipelineRunStatus.SUCCEEDED,
        dataset_id=UUID(str(persisted["dataset_id"])) if persisted.get("dataset_id") else None,
        source_id=UUID(str(persisted["source_id"])) if persisted.get("source_id") else None,
        document_id=UUID(str(persisted["document_id"])) if persisted.get("document_id") else None,
        content_hash=str(persisted["content_hash"]) if persisted.get("content_hash") else None,
        chunks=int(persisted["chunks"]) if persisted.get("chunks") is not None else None,
        entities=int(persisted["entities"]) if persisted.get("entities") is not None else None,
        relations=int(persisted["relations"]) if persisted.get("relations") is not None else None,
        deduplicated=bool(persisted["deduplicated"]) if "deduplicated" in persisted else None,
    )


async def _failed_run_error(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> SofiasMemoryError:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        error_code = run.error_code if run is not None else None
    details: dict[str, JSONValue] = {
        "run_id": str(run_id),
        "status": PipelineRunStatus.FAILED.value,
    }
    if error_code is not None:
        details["step_error_code"] = error_code
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Remember run failed.",
        details=details,
    )


async def _run_metrics(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> dict[str, Any]:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        return dict(run.metrics) if run is not None else {}


def _validate_supported_extension(filename: str | None) -> str:
    try:
        sanitized = sanitize_upload_filename(filename)
    except TextFileLoadError as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message=str(exc),
        ) from exc
    extension = PurePosixPath(sanitized).suffix.lower()
    if extension not in SUPPORTED_TEXT_FILE_EXTENSIONS:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Unsupported file extension.",
        )
    return sanitized


async def read_upload_file_bytes(file: UploadFile, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_SIZE_BYTES)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_bytes:
            raise SofiasMemoryError(
                code=ErrorCode.REQUEST_TOO_LARGE,
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                message="Uploaded file exceeds the configured source size limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def parse_metadata_json(metadata: str | None) -> dict[str, JSONValue]:
    if metadata is None or not metadata.strip():
        return {}
    try:
        decoded = json.loads(metadata)
        return METADATA_ADAPTER.validate_python(decoded)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Metadata must be a valid JSON object.",
        ) from exc
