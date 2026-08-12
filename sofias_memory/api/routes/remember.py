"""Remember text ingest route."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from pydantic import TypeAdapter, ValidationError

from sofias_memory.api.errors import SofiasMemoryError, current_request_id
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.loaders.text import TextFileLoadError, prepare_text_file_content
from sofias_memory.schemas.common import ErrorCode, JSONValue, ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.remember import RememberTextRequest, RememberTextResult
from sofias_memory.services.remember import RememberService

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
UPLOAD_READ_CHUNK_SIZE_BYTES = 1024 * 1024
METADATA_ADAPTER = TypeAdapter(dict[str, JSONValue])

router = APIRouter(tags=["remember"])


@router.post("/remember", response_model=SuccessEnvelope[RememberTextResult])
async def remember_text(
    payload: RememberTextRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    service = RememberService(
        app_settings(request.app),
        session_factory=app_postgres_session_factory(request.app),
    )
    result = await service.remember_text(payload, idempotency_key=idempotency_key)
    return SuccessEnvelope[RememberTextResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post("/remember/file", response_model=SuccessEnvelope[RememberTextResult])
async def remember_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    dataset: Annotated[str, Form()] = "main",
    metadata: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = None,
    mode: Annotated[str, Form()] = "ingest",
    wait: Annotated[bool, Form()] = True,
    force: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    settings = app_settings(request.app)
    original_bytes = await read_upload_file_bytes(
        file,
        max_bytes=settings.max_source_size_mb * 1024 * 1024,
    )
    parsed_metadata = parse_metadata_json(metadata)
    try:
        prepared_file = prepare_text_file_content(file.filename, original_bytes)
    except TextFileLoadError as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message=str(exc),
        ) from exc

    service = RememberService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
    )
    result = await service.remember_file(
        dataset=dataset.strip() or "main",
        prepared_file=prepared_file,
        metadata=parsed_metadata,
        session_id=session_id.strip() if session_id else None,
        mode=mode,
        wait=wait,
        force=force,
        idempotency_key=idempotency_key,
    )
    return SuccessEnvelope[RememberTextResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


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
