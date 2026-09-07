"""Skill management + immutable SkillRevision API routes (SM-702, ADR-0013)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.skills import (
    SKILL_PAGE_DEFAULT_LIMIT,
    SKILL_PAGE_MAX_LIMIT,
    SKILL_REVISION_PAGE_DEFAULT_LIMIT,
    SKILL_REVISION_PAGE_MAX_LIMIT,
    SkillCreateRequest,
    SkillExportResult,
    SkillImportRequest,
    SkillListResult,
    SkillResult,
    SkillRevisionCreateRequest,
    SkillRevisionListResult,
    SkillRevisionResult,
    SkillUpdateRequest,
)
from sofias_memory.services.skills import SkillService

_SKILL_NOT_FOUND_404 = error_response(
    "The target Skill does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SKILL_CREATE_CONFLICT_409 = error_response(
    "A Skill with this name already exists, in any status. Explicit create "
    "never upserts and never resolves by content hash. ErrorEnvelope with "
    "error.code=INVALID_REQUEST."
)
_SKILL_REVISION_NOT_FOUND_404 = error_response(
    "The target Skill, or the target revision within it, does not exist. "
    "ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SKILL_ROLLBACK_TARGET_422 = error_response(
    "current_revision does not reference an existing revision of this "
    "Skill. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SKILL_DEPENDENCY_UNAVAILABLE_503 = error_response(
    "The embedding provider is unavailable, or returned an unexpected "
    "vector dimension. ErrorEnvelope with error.code=DEPENDENCY_UNAVAILABLE."
)
_SKILL_MD_INVALID_422 = error_response(
    "The SKILL.md document is structurally invalid or fails the portable "
    "subset's field validation. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SKILL_MD_NAME_MISMATCH_422 = error_response(
    "The SKILL.md frontmatter name does not match the target Skill's name. "
    "ErrorEnvelope with error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["skills"])


def _skill_service(request: Request) -> SkillService:
    settings = app_settings(request.app)
    return SkillService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(settings),
    )


@router.post(
    "/skills",
    response_model=SuccessEnvelope[SkillResult],
    status_code=HTTPStatus.CREATED,
    summary="Create a Skill",
    description=(
        "Create a first-class durable procedural Skill and its first "
        "SkillRevision atomically. `name` already existing, in any status, "
        "is a conflict, never a silent upsert -- identity is decided "
        "solely by `name`, never by content hash."
    ),
    responses={
        HTTPStatus.CONFLICT: _SKILL_CREATE_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: _SKILL_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def create_skill(
    payload: SkillCreateRequest,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.create_skill(payload)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/skills/import",
    response_model=SuccessEnvelope[SkillResult],
    status_code=HTTPStatus.CREATED,
    summary="Import a standalone SKILL.md as a new Skill",
    description=(
        "Parse a standalone SKILL.md document and create a new Skill and "
        "its first SkillRevision atomically -- equivalent to `POST "
        "/skills`, sourced from SKILL.md instead of structured fields. "
        "`name` already existing, in any status, is always a conflict, "
        "even when the content is semantically identical to an existing "
        "Skill: this route never resolves by content hash, never safe "
        "replays, and never creates a revision implicitly."
    ),
    responses={
        HTTPStatus.UNPROCESSABLE_ENTITY: _SKILL_MD_INVALID_422,
        HTTPStatus.CONFLICT: _SKILL_CREATE_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: _SKILL_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def import_skill(
    payload: SkillImportRequest,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.import_skill(payload.content)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/skills",
    response_model=SuccessEnvelope[SkillListResult],
    summary="List Skills",
    description=(
        "List Skills, paginated, optionally filtered by status. Never returns `procedure`."
    ),
)
async def list_skills(
    request: Request,
    limit: int = Query(default=SKILL_PAGE_DEFAULT_LIMIT, ge=1, le=SKILL_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    status: Annotated[SkillStatus | None, Query()] = None,
) -> SuccessEnvelope[SkillListResult]:
    service = _skill_service(request)
    result = await service.list_skills(limit=limit, offset=offset, status=status)
    return SuccessEnvelope[SkillListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/skills/{skill_uuid}",
    response_model=SuccessEnvelope[SkillResult],
    summary="Get Skill details",
    description=(
        "Get one Skill's management detail by its structural skill_uuid. "
        "Allowed for both active and archived Skills. Never returns "
        "`procedure` -- fetch `GET .../revisions/{revision}` for that."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404},
)
async def get_skill(
    skill_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.get_skill(skill_uuid)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.patch(
    "/skills/{skill_uuid}",
    response_model=SuccessEnvelope[SkillResult],
    summary="Roll back a Skill's current revision",
    description=(
        "Strictly administrative: the only mutable field is "
        "`current_revision`, repointing to an existing historical revision "
        "of this Skill -- never creates a revision, never copies or "
        "re-embeds content. Idempotent when `current_revision` already "
        "matches. Permitted even when the Skill is archived."
    ),
    responses={
        HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404,
        HTTPStatus.UNPROCESSABLE_ENTITY: _SKILL_ROLLBACK_TARGET_422,
    },
)
async def update_skill(
    skill_uuid: UUID,
    payload: SkillUpdateRequest,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.update_skill(skill_uuid, payload)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/skills/{skill_uuid}/archive",
    response_model=SuccessEnvelope[SkillResult],
    summary="Archive a Skill",
    description=(
        "Idempotent discovery filter, not an admission barrier: active -> "
        "archived, archived -> archived (no-op). A future semantic resolve "
        "endpoint will never return an archived Skill, but every "
        "management operation (`GET`, create revision, `PATCH "
        "current_revision`) remains available. Never touches "
        "`current_revision`."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404},
)
async def archive_skill(
    skill_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.archive_skill(skill_uuid)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/skills/{skill_uuid}/restore",
    response_model=SuccessEnvelope[SkillResult],
    summary="Restore a Skill",
    description=(
        "Idempotent: archived -> active, active -> active (no-op). "
        "Preserves whatever `current_revision` the Skill has *at the "
        "moment of restore* -- never resets it to what it was before the "
        "archive, even if new revisions were created while archived."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404},
)
async def restore_skill(
    skill_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SkillResult]:
    service = _skill_service(request)
    result = await service.restore_skill(skill_uuid)
    return SuccessEnvelope[SkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/skills/{skill_uuid}/revisions",
    response_model=SuccessEnvelope[SkillRevisionResult],
    summary="Create a SkillRevision",
    description=(
        "Create a new SkillRevision on an already-identified Skill, or "
        "resolve a safe replay: semantically new content returns `201`; "
        "content identical (by canonical hash) to an existing revision of "
        "this Skill returns `200` with that existing revision, and "
        "`current_revision` on the Skill is left unchanged. Permitted even "
        "when the Skill is archived -- archiving is a discovery filter, "
        "never a write admission barrier."
    ),
    responses={
        HTTPStatus.OK: error_response(
            "Safe replay: semantically identical content already exists as "
            "a revision of this Skill. Returns 200 with that existing "
            "revision -- current_revision is not modified."
        ),
        HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _SKILL_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def create_skill_revision(
    skill_uuid: UUID,
    payload: SkillRevisionCreateRequest,
    request: Request,
    response: Response,
) -> SuccessEnvelope[SkillRevisionResult]:
    service = _skill_service(request)
    result, created = await service.create_revision(skill_uuid, payload)
    response.status_code = HTTPStatus.CREATED if created else HTTPStatus.OK
    return SuccessEnvelope[SkillRevisionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/skills/{skill_uuid}/revisions/import",
    response_model=SuccessEnvelope[SkillRevisionResult],
    summary="Import a standalone SKILL.md as a new SkillRevision",
    description=(
        "Parse a standalone SKILL.md document and create a new "
        "SkillRevision on the identified Skill -- equivalent to `POST "
        ".../revisions`, sourced from SKILL.md instead of structured "
        "fields, and following the exact same safe-replay rule: `201` for "
        "semantically new content, `200` with the existing revision "
        "(current_revision unchanged) for content identical by canonical "
        "hash to a revision this Skill already has. The frontmatter `name` "
        "must equal the target Skill's name exactly; a mismatch is `422`, "
        "never an implicit rename or redirect. Permitted even when the "
        "Skill is archived."
    ),
    responses={
        HTTPStatus.OK: error_response(
            "Safe replay: semantically identical content already exists as "
            "a revision of this Skill. Returns 200 with that existing "
            "revision -- current_revision is not modified."
        ),
        HTTPStatus.UNPROCESSABLE_ENTITY: _SKILL_MD_NAME_MISMATCH_422,
        HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _SKILL_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def import_skill_revision(
    skill_uuid: UUID,
    payload: SkillImportRequest,
    request: Request,
    response: Response,
) -> SuccessEnvelope[SkillRevisionResult]:
    service = _skill_service(request)
    result, created = await service.import_revision(skill_uuid, payload.content)
    response.status_code = HTTPStatus.CREATED if created else HTTPStatus.OK
    return SuccessEnvelope[SkillRevisionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/skills/{skill_uuid}/revisions",
    response_model=SuccessEnvelope[SkillRevisionListResult],
    summary="List SkillRevisions",
    description=(
        "List a Skill's revisions, paginated, ordered by revision "
        "ascending. Lightweight: never returns `procedure` or `metadata`. "
        "Allowed for both active and archived Skills."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_NOT_FOUND_404},
)
async def list_skill_revisions(
    skill_uuid: UUID,
    request: Request,
    limit: int = Query(
        default=SKILL_REVISION_PAGE_DEFAULT_LIMIT, ge=1, le=SKILL_REVISION_PAGE_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
) -> SuccessEnvelope[SkillRevisionListResult]:
    service = _skill_service(request)
    result = await service.list_revisions(skill_uuid, limit=limit, offset=offset)
    return SuccessEnvelope[SkillRevisionListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/skills/{skill_uuid}/revisions/{revision}",
    response_model=SuccessEnvelope[SkillRevisionResult],
    summary="Get SkillRevision detail",
    description=(
        "Full detail for one specific revision, including `procedure` -- "
        "the only endpoint that ever returns it. Allowed for both active "
        "and archived Skills."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_REVISION_NOT_FOUND_404},
)
async def get_skill_revision(
    skill_uuid: UUID,
    revision: int,
    request: Request,
) -> SuccessEnvelope[SkillRevisionResult]:
    service = _skill_service(request)
    result = await service.get_revision(skill_uuid, revision)
    return SuccessEnvelope[SkillRevisionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/skills/{skill_uuid}/revisions/{revision}/export",
    response_model=SuccessEnvelope[SkillExportResult],
    summary="Export a SkillRevision as standalone SKILL.md",
    description=(
        "Serialize one specific revision's persisted semantic content as a "
        "standalone SKILL.md document, inside the standard SuccessEnvelope. "
        "Deterministic over the persisted content: the same revision "
        "always produces the same `content`/`content_sha256` -- never a "
        "promise of byte-identity with whatever SKILL.md was originally "
        "imported. `content_sha256` is the persisted SkillRevision's own "
        "semantic hash, not a digest of the exported bytes. Allowed for "
        "both active and archived Skills."
    ),
    responses={HTTPStatus.NOT_FOUND: _SKILL_REVISION_NOT_FOUND_404},
)
async def export_skill_revision(
    skill_uuid: UUID,
    revision: int,
    request: Request,
) -> SuccessEnvelope[SkillExportResult]:
    service = _skill_service(request)
    result = await service.export_revision(skill_uuid, revision)
    return SuccessEnvelope[SkillExportResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
