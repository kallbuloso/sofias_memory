from __future__ import annotations

import os

import pytest

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jReadinessChecker,
    create_neo4j_resource_from_settings,
)

NEO4J_READINESS_TESTS_ENV = "SOFIAS_MEMORY_RUN_NEO4J_READINESS_TESTS"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_neo4j_readiness_against_configured_database() -> None:
    if os.environ.get(NEO4J_READINESS_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_READINESS_TESTS_ENV}=1 to run the Neo4j readiness test")

    resource = create_neo4j_resource_from_settings(load_settings())
    try:
        result = await Neo4jReadinessChecker(resource).check()
    finally:
        await resource.close()

    assert result.ready is True
