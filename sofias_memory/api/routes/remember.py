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
from sofias_memory.api.openapi_responses import (
    DATASET_NOT_FOUND_404,
    IDEMPOTENCY_OR_DATASET_CONFLICT_409,
    RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
    WORKER_DISABLED_503,
)
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    InvalidSessionIdError,
    PipelineRunStatus,
    PipelineType,
    SessionStatus,
    normalize_session_id,
)
from sofias_memory.infrastructure.postgres.models import Dataset, Session
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
from sofias_memory.schemas.common import (
    ErrorCode,
    JSONValue,
    ResponseMeta,
    SuccessEnvelope,
    utc_now,
)
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
from sofias_memory.services.session_entries import session_archived_error

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Optional retry-safety key for this write. Reusing the same key with the "
    "same logical request returns the original PipelineRun instead of creating "
    "duplicate work; reusing it for different work returns an idempotency "
    "conflict. Keys starting with 'sys:' are reserved. Leave blank for ordinary "
    "manual testing."
)
RETRY_SAFETY_DESCRIPTION = (
    "\n\n**Retry safety:** clients may optionally send an `Idempotency-Key` "
    "header (see the canonical /openapi.json for its full parameter "
    "documentation -- it is omitted from this human-facing page for "
    "readability). Reusing the same key for the same logical request returns "
    "the original run instead of creating duplicate work; reusing it for "
    "different work returns a conflict. Ordinary manual testing does not "
    "require it."
)
UPLOAD_READ_CHUNK_SIZE_BYTES = 1024 * 1024
METADATA_ADAPTER = TypeAdapter(dict[str, JSONValue])

router = APIRouter(tags=["remember"])


@router.post(
    "/remember",
    response_model=SuccessEnvelope[RememberTextResult],
    summary="Remember text content",
    description=(
        "Store raw text as a new source in a dataset. With `mode=ingest` (default) "
        "the content is stored as-is for a later Cognify run -- no LLM cost. With "
        "`mode=full` it is also chunked, embedded, and processed into entities and "
        "relations immediately. Creates a durable PipelineRun; use `wait=false` for "
        "an immediate `202` or `wait=true` to wait for the terminal result."
        + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: IDEMPOTENCY_OR_DATASET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def remember_text(
    payload: RememberTextRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
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
        prepare=_remember_preparation_hook(payload.dataset, payload.session_id),
    )

    status = await _await_if_requested(
        session_factory, settings=settings, outcome=outcome, wait=payload.wait
    )
    return await _respond(response, session_factory=session_factory, outcome=outcome, status=status)


@router.post(
    "/remember/url",
    response_model=SuccessEnvelope[RememberTextResult],
    summary="Remember an HTTPS URL",
    description=(
        "Fetch a single HTTPS URL and store its content as a new source in a "
        "dataset. The fetch happens asynchronously in the worker, not during this "
        "request; SSRF guards (loopback/link-local/private-network/cloud-metadata) "
        "apply to the actual fetch. Same `mode`/`wait` semantics as text Remember."
        + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: IDEMPOTENCY_OR_DATASET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def remember_url(
    payload: RememberUrlRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
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
        prepare=_remember_preparation_hook(payload.dataset, payload.session_id),
    )

    status = await _await_if_requested(
        session_factory, settings=settings, outcome=outcome, wait=payload.wait
    )
    return await _respond(response, session_factory=session_factory, outcome=outcome, status=status)


@router.post(
    "/remember/file",
    response_model=SuccessEnvelope[RememberTextResult],
    summary="Upload a file to memory",
    description=(
        "Upload a supported text-bearing file (multipart/form-data) as a new "
        "source in a dataset. Same `mode`/`wait` semantics as text Remember. "
        "`metadata` is a JSON object encoded as a string form field, not native "
        "JSON, because multipart form fields are always strings." + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: IDEMPOTENCY_OR_DATASET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def remember_file(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="The file to remember.")],
    dataset: Annotated[
        str,
        Form(description="Target dataset slug. Created automatically only if it is 'main'."),
    ] = "main",
    metadata: Annotated[
        str | None,
        Form(
            description=(
                'Optional metadata as a JSON object encoded as a string, e.g. \'{"k":"v"}\'.'
            )
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Form(
            description=(
                "Optional caller-supplied Session external key. When present, the "
                "Session is resolved or lazily created (rejected if archived) and "
                "the resulting PipelineRun is associated with it -- this is a "
                "first-class association, not mere correlation metadata."
            )
        ),
    ] = None,
    mode: Annotated[
        str,
        Form(
            description=(
                "'ingest' stores the file as-is for a later Cognify run. 'full' also "
                "chunks, embeds, and extracts entities/relations immediately."
            )
        ),
    ] = "ingest",
    wait: Annotated[
        bool,
        Form(
            description=(
                "If true, wait for this run to reach a terminal state before responding. "
                "If false, return as soon as the run is durably queued."
            )
        ),
    ] = True,
    force: Annotated[
        bool,
        Form(
            description=(
                "Re-process even if identical content was already remembered for this dataset."
            )
        ),
    ] = False,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
) -> SuccessEnvelope[RememberTextResult]:
    validate_remember_mode(mode)
    normalized_session_id = _normalize_form_session_id(session_id)
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
        session_id=normalized_session_id,
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
        prepare=_remember_preparation_hook(dataset_slug, normalized_session_id),
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


def _normalize_form_session_id(session_id: str | None) -> str | None:
    """SM-605 SS 7: File's multipart form has no Pydantic request body, so
    the shared normalization primitive is applied explicitly here -- never
    `.strip()` ad hoc -- yielding byte-for-byte the same accept/reject rules
    Text and URL get for free from their schema's field_validator."""

    try:
        return normalize_session_id(session_id)
    except InvalidSessionIdError as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message=str(exc),
        ) from exc


def _remember_preparation_hook(
    dataset_slug: str, normalized_session_id: str | None
) -> PreparationHook:
    """SM-605 SS 13/14: resolves/validates the Dataset first -- a Session is
    never lazily materialized for a request whose Dataset turns out to be
    invalid -- then, only if a normalized `session_id` was supplied,
    resolves/lazily-creates the Session and locks its row (the same
    `get_or_create_by_key` + `get_by_id_for_update` admission barrier
    Recall's own Session admission uses, SM-604) before returning both
    targets to the shared submission transaction. Everything here runs
    inside that one transaction; an archived Session or an invalid Dataset
    both roll the whole attempt back with zero PipelineRun/PipelineStep rows
    created."""

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

        session_uuid = None
        if normalized_session_id is not None:
            now = utc_now()
            candidate = Session(
                id=uuid4(),
                key=normalized_session_id,
                name=None,
                status=SessionStatus.ACTIVE,
                metadata_={},
                created_at=now,
                updated_at=now,
                archived_at=None,
            )
            resolved = await postgres_uow.sessions.get_or_create_by_key(candidate)
            session = await postgres_uow.sessions.get_by_id_for_update(resolved.id)
            assert session is not None  # noqa: S101 - just resolved/created above
            if session.status == SessionStatus.ARCHIVED:
                raise session_archived_error(session.id)
            session_uuid = session.id

        return SubmissionTargets(dataset_id=dataset.id, source_id=None, session_id=session_uuid)

    return prepare


async def _respond(
    response: Response,
    *,
    session_factory: AsyncSessionFactory,
    outcome: SubmissionOutcome,
    status: PipelineRunStatus,
) -> SuccessEnvelope[RememberTextResult]:
    if status == PipelineRunStatus.SUCCEEDED:
        result = await _succeeded_result(
            session_factory, run_id=outcome.run_id, session_uuid=outcome.session_id
        )
    elif status == PipelineRunStatus.FAILED:
        raise await _failed_run_error(session_factory, run_id=outcome.run_id)
    else:
        result = RememberTextResult(
            run_id=outcome.run_id, status=status, session_uuid=outcome.session_id
        )
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
    session_uuid: UUID | None,
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
        session_uuid=session_uuid,
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
