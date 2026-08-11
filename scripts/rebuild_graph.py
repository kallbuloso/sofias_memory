"""Operational script for rebuilding the Neo4j projection from PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.services.graph_rebuild_service import GraphRebuildResult, GraphRebuildService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the Sofias Memory Neo4j projection.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--dataset", type=UUID, help="Rebuild one dataset projection by UUID.")
    scope.add_argument(
        "--all",
        action="store_true",
        dest="all_datasets",
        help="Rebuild all datasets.",
    )
    parser.add_argument(
        "--confirm-all",
        action="store_true",
        help="Required with --all because global rebuild removes the full Sofias projection.",
    )
    args = parser.parse_args()
    if args.all_datasets and not args.confirm_all:
        parser.error("--all requires --confirm-all")
    return args


async def run_rebuild(args: argparse.Namespace) -> GraphRebuildResult:
    settings = load_settings()
    postgres_engine = create_async_engine_from_settings(settings)
    neo4j_resource = create_neo4j_resource_from_settings(settings)
    try:
        service = GraphRebuildService(
            session_factory=create_session_factory(postgres_engine),
            neo4j_resource=neo4j_resource,
            projection=Neo4jProjection(neo4j_resource),
        )
        if args.dataset is not None:
            return await service.rebuild_dataset(args.dataset)
        return await service.rebuild_all()
    finally:
        await neo4j_resource.close()
        await dispose_async_engine(postgres_engine)


def print_result(result: GraphRebuildResult) -> None:
    parts = [
        f"scope={result.scope}",
        f"datasets={result.datasets}",
        f"entities={result.entities}",
        f"chunks={result.chunks}",
        f"entity_mentions={result.entity_mentions}",
        f"relations={result.relations}",
        f"next_relationships={result.next_relationships}",
    ]
    if result.dataset_id is not None:
        parts.insert(1, f"dataset_id={result.dataset_id}")
    print("Graph rebuild complete: " + " ".join(parts))


async def main() -> None:
    result = await run_rebuild(parse_args())
    print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
