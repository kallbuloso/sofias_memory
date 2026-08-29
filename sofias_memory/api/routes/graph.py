"""Read-only graph API routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import DATASET_NOT_FOUND_404, error_response
from sofias_memory.infrastructure.neo4j.graph_read import Neo4jGraphRead
from sofias_memory.lifespan import app_neo4j_resource, app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.graph import GraphPathResult, GraphSchemaResult, GraphSubgraphResult
from sofias_memory.services.graph_read import GraphReadService

_GRAPH_NEO4J_UNAVAILABLE_503 = error_response(
    "Neo4j graph traversal is unavailable. ErrorEnvelope with error.code=DEPENDENCY_UNAVAILABLE."
)
_GRAPH_ENTITY_OR_DATASET_NOT_FOUND_404 = error_response(
    "The dataset or entity does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_GRAPH_DEPTH_OUT_OF_RANGE_400 = error_response(
    "The requested depth is outside the supported range. ErrorEnvelope with "
    "error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["graph"])


@router.get(
    "/graph/schema",
    response_model=SuccessEnvelope[GraphSchemaResult],
    summary="Get graph schema summary",
    description=(
        "Get the distinct entity types and relation predicates present in a dataset's graph."
    ),
    responses={
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _GRAPH_NEO4J_UNAVAILABLE_503,
    },
)
async def graph_schema(
    request: Request,
    dataset: Annotated[str, Query(description="Dataset slug to read the graph from.")] = "main",
) -> SuccessEnvelope[GraphSchemaResult]:
    service = _build_service(request)
    result = await service.schema(dataset_slug=dataset)
    return SuccessEnvelope[GraphSchemaResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/graph/subgraph",
    response_model=SuccessEnvelope[GraphSubgraphResult],
    summary="Get an entity's subgraph",
    description=(
        "Get the neighborhood of relations and entities around one entity, up to a depth limit."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: _GRAPH_DEPTH_OUT_OF_RANGE_400,
        HTTPStatus.NOT_FOUND: _GRAPH_ENTITY_OR_DATASET_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _GRAPH_NEO4J_UNAVAILABLE_503,
    },
)
async def graph_subgraph(
    request: Request,
    entity_id: UUID,
    dataset: Annotated[str, Query(description="Dataset slug to read the graph from.")] = "main",
    depth: Annotated[
        int, Query(description="Maximum number of relation hops to traverse from entity_id.")
    ] = 2,
) -> SuccessEnvelope[GraphSubgraphResult]:
    service = _build_service(request)
    result = await service.subgraph(dataset_slug=dataset, entity_id=entity_id, depth=depth)
    return SuccessEnvelope[GraphSubgraphResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/graph/path",
    response_model=SuccessEnvelope[GraphPathResult],
    summary="Find a path between two entities",
    description="Find the shortest relation path between two entities, up to a maximum depth.",
    responses={
        HTTPStatus.BAD_REQUEST: _GRAPH_DEPTH_OUT_OF_RANGE_400,
        HTTPStatus.NOT_FOUND: _GRAPH_ENTITY_OR_DATASET_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _GRAPH_NEO4J_UNAVAILABLE_503,
    },
)
async def graph_path(
    request: Request,
    from_: Annotated[UUID, Query(alias="from", description="Starting entity id.")],
    to: Annotated[UUID, Query(description="Target entity id.")],
    dataset: Annotated[str, Query(description="Dataset slug to read the graph from.")] = "main",
    max_depth: Annotated[int, Query(description="Maximum number of relation hops to search.")] = 4,
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
