"""Agent management API routes (SM-802/SM-803, ADR-0014).

Nine operations: create/list/get/update/archive/restore (SM-802) plus the
explicit Agent<->Skill association -- list/set/remove (SM-803). No
Agent<->Session endpoint (SM-804), no resolve, no execution-shaped route of
any kind -- those remain permanently or deferredly out of scope (Feature
Contract SS 18, 22, 38-39).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.domain import AgentStatus
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.agent_skills import (
    AgentSkillListResult,
    AgentSkillResult,
    AgentSkillSetRequest,
)
from sofias_memory.schemas.agents import (
    AGENT_PAGE_DEFAULT_LIMIT,
    AGENT_PAGE_MAX_LIMIT,
    AgentCreateRequest,
    AgentListResult,
    AgentResult,
    AgentUpdateRequest,
)
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.services.agent_skills import AgentSkillService
from sofias_memory.services.agents import AgentService

_AGENT_NOT_FOUND_404 = error_response(
    "The target Agent does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_AGENT_CREATE_CONFLICT_409 = error_response(
    "An Agent with this name already exists, in any status. Explicit create "
    "never upserts. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_AGENT_OR_SKILL_NOT_FOUND_404 = error_response(
    "The target Agent, or the target Skill, does not exist. ErrorEnvelope "
    "with error.code=INVALID_REQUEST."
)
_INVALID_PINNED_REVISION_422 = error_response(
    "pinned_revision does not reference an existing revision of this "
    "Skill. ErrorEnvelope with error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["agents"])


def _agent_service(request: Request) -> AgentService:
    return AgentService(session_factory=app_postgres_session_factory(request.app))


def _agent_skill_service(request: Request) -> AgentSkillService:
    return AgentSkillService(session_factory=app_postgres_session_factory(request.app))


@router.post(
    "/agents",
    response_model=SuccessEnvelope[AgentResult],
    status_code=HTTPStatus.CREATED,
    summary="Create an Agent",
    description=(
        "Create a durable Agent Profile -- a management resource, never a "
        "runtime. Sofias Memory never executes an Agent, selects a "
        "provider/model on its behalf, or manages a provider session. "
        "`name` already existing, in any status, is a conflict, never a "
        "silent upsert."
    ),
    responses={HTTPStatus.CONFLICT: _AGENT_CREATE_CONFLICT_409},
)
async def create_agent(
    payload: AgentCreateRequest,
    request: Request,
) -> SuccessEnvelope[AgentResult]:
    service = _agent_service(request)
    result = await service.create_agent(payload)
    return SuccessEnvelope[AgentResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/agents",
    response_model=SuccessEnvelope[AgentListResult],
    summary="List Agents",
    description=(
        "List Agent Profiles, paginated. Without a `status` filter, returns "
        "only `active` Agents -- a deliberate divergence from Session/Skill "
        "listing, which default to every status. `instructions` and "
        "`metadata` are never included in list items (progressive "
        "disclosure); read `GET /agents/{agent_uuid}` for the full detail."
    ),
)
async def list_agents(
    request: Request,
    limit: int = Query(default=AGENT_PAGE_DEFAULT_LIMIT, ge=1, le=AGENT_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    status: Annotated[
        AgentStatus,
        Query(description="Defaults to active -- there is no all-status option."),
    ] = AgentStatus.ACTIVE,
) -> SuccessEnvelope[AgentListResult]:
    service = _agent_service(request)
    result = await service.list_agents(limit=limit, offset=offset, status=status)
    return SuccessEnvelope[AgentListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/agents/{agent_uuid}",
    response_model=SuccessEnvelope[AgentResult],
    summary="Get Agent details",
    description=(
        "Get one Agent's full management detail (including `instructions`/"
        "`metadata`) by its structural `agent_uuid`. Works for both `active` "
        "and `archived` Agents."
    ),
    responses={HTTPStatus.NOT_FOUND: _AGENT_NOT_FOUND_404},
)
async def get_agent(
    agent_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[AgentResult]:
    service = _agent_service(request)
    result = await service.get_agent(agent_uuid)
    return SuccessEnvelope[AgentResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.patch(
    "/agents/{agent_uuid}",
    response_model=SuccessEnvelope[AgentResult],
    summary="Update an Agent Profile",
    description=(
        "Update `display_name`/`description`/`instructions`/`metadata`. "
        "`name` and `status` are immutable here -- `status` changes only "
        "via archive/restore. `metadata` is replaced wholesale, never deep-"
        "merged. Permitted even when the Agent is archived: archive is a "
        "discovery filter, never an admission barrier over management."
    ),
    responses={HTTPStatus.NOT_FOUND: _AGENT_NOT_FOUND_404},
)
async def update_agent(
    agent_uuid: UUID,
    payload: AgentUpdateRequest,
    request: Request,
) -> SuccessEnvelope[AgentResult]:
    service = _agent_service(request)
    result = await service.update_agent(agent_uuid, payload)
    return SuccessEnvelope[AgentResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/agents/{agent_uuid}/archive",
    response_model=SuccessEnvelope[AgentResult],
    summary="Archive an Agent",
    description=(
        "Idempotent discovery/availability filter: `active -> archived`, "
        "`archived -> archived` (no-op, no timestamp churn). Sofias Memory "
        "never executes an Agent, so archive cannot and does not claim to "
        "stop any external runtime activity -- it only removes the Agent "
        "from the default `GET /agents` listing."
    ),
    responses={HTTPStatus.NOT_FOUND: _AGENT_NOT_FOUND_404},
)
async def archive_agent(
    agent_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[AgentResult]:
    service = _agent_service(request)
    result = await service.archive_agent(agent_uuid)
    return SuccessEnvelope[AgentResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/agents/{agent_uuid}/restore",
    response_model=SuccessEnvelope[AgentResult],
    summary="Restore an Agent",
    description="Idempotent: `archived -> active`, `active -> active` (no-op, no timestamp churn).",
    responses={HTTPStatus.NOT_FOUND: _AGENT_NOT_FOUND_404},
)
async def restore_agent(
    agent_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[AgentResult]:
    service = _agent_service(request)
    result = await service.restore_agent(agent_uuid)
    return SuccessEnvelope[AgentResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/agents/{agent_uuid}/skills",
    response_model=SuccessEnvelope[AgentSkillListResult],
    summary="List an Agent's associated Skills",
    description=(
        "Management/disclosure list of Skills explicitly associated with "
        "this Agent -- not semantic resolve, never chooses or loads a "
        "`procedure`. Includes archived Skills. Permitted for both active "
        "and archived Agents. Not paginated."
    ),
    responses={HTTPStatus.NOT_FOUND: _AGENT_NOT_FOUND_404},
)
async def list_agent_skills(
    agent_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[AgentSkillListResult]:
    service = _agent_skill_service(request)
    result = await service.list_skills(agent_uuid)
    return SuccessEnvelope[AgentSkillListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.put(
    "/agents/{agent_uuid}/skills/{skill_uuid}",
    response_model=SuccessEnvelope[AgentSkillResult],
    summary="Associate a Skill with an Agent, with an optional revision pin",
    description=(
        "Idempotent set/upsert. `pinned_revision` omitted or `null` means "
        "the association follows the Skill's current revision live; an "
        "integer pins it to exactly that revision of this Skill (never a "
        "global revision number). Never authorization, never Skill "
        "execution. Permitted for both active and archived Agents/Skills."
    ),
    responses={
        HTTPStatus.NOT_FOUND: _AGENT_OR_SKILL_NOT_FOUND_404,
        HTTPStatus.UNPROCESSABLE_ENTITY: _INVALID_PINNED_REVISION_422,
    },
)
async def set_agent_skill(
    agent_uuid: UUID,
    skill_uuid: UUID,
    payload: AgentSkillSetRequest,
    request: Request,
) -> SuccessEnvelope[AgentSkillResult]:
    service = _agent_skill_service(request)
    result = await service.set_skill(agent_uuid, skill_uuid, payload)
    return SuccessEnvelope[AgentSkillResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.delete(
    "/agents/{agent_uuid}/skills/{skill_uuid}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Remove an Agent<->Skill association",
    description=(
        "Idempotent: removing a non-existent association still succeeds. "
        "The target Skill itself must exist -- the path addresses "
        '`{skill_uuid}` as a resource, distinct from "no association '
        'exists". Never modifies the Skill or any SkillRevision.'
    ),
    responses={HTTPStatus.NOT_FOUND: _AGENT_OR_SKILL_NOT_FOUND_404},
)
async def remove_agent_skill(
    agent_uuid: UUID,
    skill_uuid: UUID,
    request: Request,
) -> None:
    service = _agent_skill_service(request)
    await service.remove_skill(agent_uuid, skill_uuid)
