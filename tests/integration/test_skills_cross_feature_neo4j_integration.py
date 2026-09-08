"""Real-Neo4j SM-705 proof that Skills never touch the graph.

Executes a representative matrix of Skill write and read operations
(structured create/revision, safe replay, ``current_revision`` rollback,
archive, revision-while-archived, restore, SKILL.md import/revision-import/
replay, list/detail/revision-detail/export/resolve) against real
PostgreSQL only, then proves Neo4j's total node/relationship counts are
unchanged and that no Skill-shaped label, relationship type, or
identity-bearing property was ever created -- Skills are never projected
(ADR-0013, Feature Contract SS 5). Requires migrations already applied
through 0014 and a real, reachable Neo4j instance.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import Neo4jResource, create_neo4j_resource_from_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Skill
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.interoperability.skill_md import parse_skill_markdown, serialize_skill_markdown
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillResolveRequest,
    SkillRevisionCreateRequest,
    SkillUpdateRequest,
)
from sofias_memory.services.skills import SkillService

NEO4J_SKILLS_TESTS_ENV = "SOFIAS_MEMORY_RUN_SKILLS_NEO4J_TESTS"
POSTGRES_SKILLS_CROSS_FEATURE_ENV = "SOFIAS_MEMORY_RUN_SKILLS_CROSS_FEATURE_POSTGRES_TESTS"
EMBEDDING_DIMENSIONS = 3072


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _require_real_neo4j() -> None:
    if os.environ.get(NEO4J_SKILLS_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_SKILLS_TESTS_ENV}=1 to run real-Neo4j Skill isolation tests")


def _postgres_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_CROSS_FEATURE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_CROSS_FEATURE_ENV}=1 to run this suite")
    if not env.get("DATABASE_URL", "").strip():
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return env["DATABASE_URL"]


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _postgres_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def skill_service(session_factory: AsyncSessionFactory) -> SkillService:
    return SkillService(
        load_settings(), embedding_client=FakeEmbeddingClient(), session_factory=session_factory
    )


async def cleanup_skills(session_factory: AsyncSessionFactory, skill_ids: set[Any]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]


async def total_node_count(resource: Neo4jResource) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n) RETURN count(n) AS count", {}, database_=resource.database
    )
    return int(result_records(result)[0]["count"])


async def total_relationship_count(resource: Neo4jResource) -> int:
    result = await resource.driver.execute_query(
        "MATCH ()-[r]->() RETURN count(r) AS count", {}, database_=resource.database
    )
    return int(result_records(result)[0]["count"])


async def nodes_with_property_value(resource: Neo4jResource, value: str) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n) WHERE any(k IN keys(n) WHERE n[k] = $value) RETURN count(n) AS count",
        {"value": value},
        database_=resource.database,
    )
    return int(result_records(result)[0]["count"])


async def labels_matching_skill(resource: Neo4jResource) -> list[str]:
    result = await resource.driver.execute_query(
        "CALL db.labels() YIELD label WHERE toLower(label) CONTAINS 'skill' RETURN label",
        {},
        database_=resource.database,
    )
    return [str(record["label"]) for record in result_records(result)]


async def relationship_types_matching_skill(resource: Neo4jResource) -> list[str]:
    result = await resource.driver.execute_query(
        "CALL db.relationshipTypes() YIELD relationshipType "
        "WHERE toLower(relationshipType) CONTAINS 'skill' RETURN relationshipType",
        {},
        database_=resource.database,
    )
    return [str(record["relationshipType"]) for record in result_records(result)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_skill_operations_matrix_never_projects_to_neo4j(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    _require_real_neo4j()
    resource = create_neo4j_resource_from_settings(load_settings())
    resolver = skill_service(postgres_session_factory)
    skill_ids: set[Any] = set()
    name = unique_name("neo4j-isolation")

    try:
        nodes_before = await total_node_count(resource)
        relationships_before = await total_relationship_count(resource)

        created = await resolver.create_skill(
            SkillCreateRequest(name=name, description="d", procedure="p")  # type: ignore[call-arg]
        )
        skill_ids.add(created.skill_uuid)
        skill_id = created.skill_uuid

        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]
        await resolver.update_skill(skill_id, SkillUpdateRequest(current_revision=1))
        await resolver.archive_skill(skill_id)
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d3", procedure="p3")
        )  # type: ignore[call-arg]
        await resolver.restore_skill(skill_id)

        exported = await resolver.export_revision(skill_id, 1)
        parsed = parse_skill_markdown(exported.content)
        import_name = unique_name("neo4j-isolation-import")
        imported = await resolver.import_skill(
            serialize_skill_markdown(name=import_name, content=parsed.content)
        )
        skill_ids.add(imported.skill_uuid)
        import_content = await resolver.export_revision(imported.skill_uuid, 1)
        await resolver.import_revision(imported.skill_uuid, import_content.content)

        await resolver.list_skills(limit=10, offset=0, status=None)
        await resolver.get_skill(skill_id)
        await resolver.list_revisions(skill_id, limit=10, offset=0)
        await resolver.get_revision(skill_id, 1)
        await resolver.export_revision(skill_id, 1)
        await resolver.resolve(SkillResolveRequest(query="anything", top_k=5))

        nodes_after = await total_node_count(resource)
        relationships_after = await total_relationship_count(resource)

        assert nodes_after == nodes_before
        assert relationships_after == relationships_before
        assert await labels_matching_skill(resource) == []
        assert await relationship_types_matching_skill(resource) == []
        assert await nodes_with_property_value(resource, str(skill_id)) == 0
        assert await nodes_with_property_value(resource, name) == 0
        assert await nodes_with_property_value(resource, str(imported.skill_uuid)) == 0
        assert await nodes_with_property_value(resource, import_name) == 0
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)
        await resource.close()
