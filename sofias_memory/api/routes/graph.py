"""Read-only graph API routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.infrastructure.neo4j.graph_read import Neo4jGraphRead
from sofias_memory.lifespan import app_neo4j_resource, app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.graph import GraphPathResult, GraphSchemaResult, GraphSubgraphResult
from sofias_memory.services.graph_read import GraphReadService

router = APIRouter(tags=["graph"])


@router.get("/graph/schema", response_model=SuccessEnvelope[GraphSchemaResult])
async def graph_schema(
    request: Request,
    dataset: Annotated[str, Query()] = "main",
) -> SuccessEnvelope[GraphSchemaResult]:
    service = _build_service(request)
    result = await service.schema(dataset_slug=dataset)
    return SuccessEnvelope[GraphSchemaResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/graph/subgraph", response_model=SuccessEnvelope[GraphSubgraphResult])
async def graph_subgraph(
    request: Request,
    entity_id: UUID,
    dataset: Annotated[str, Query()] = "main",
    depth: Annotated[int, Query()] = 2,
) -> SuccessEnvelope[GraphSubgraphResult]:
    service = _build_service(request)
    result = await service.subgraph(dataset_slug=dataset, entity_id=entity_id, depth=depth)
    return SuccessEnvelope[GraphSubgraphResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/graph/path", response_model=SuccessEnvelope[GraphPathResult])
async def graph_path(
    request: Request,
    from_: Annotated[UUID, Query(alias="from")],
    to: Annotated[UUID, Query()],
    dataset: Annotated[str, Query()] = "main",
    max_depth: Annotated[int, Query()] = 4,
) -> SuccessEnvelope[GraphPathResult]:
    service = _build_service(request)
    result = await service.path(
        dataset_slug=dataset,
        from_entity_id=from_,
        to_entity_id=to,
        max_depth=max_depth,
    )
    return SuccessEnvelope[GraphPathResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


def _build_service(request: Request) -> GraphReadService:
    settings = app_settings(request.app)
    return GraphReadService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        graph_client=Neo4jGraphRead(app_neo4j_resource(request.app)),
    )
