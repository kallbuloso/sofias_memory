"""Real-PostgreSQL + pgvector tests for the SM-1003 Typed Cognitive Recall
surface (ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory SS 14).

Proves, against a real PostgreSQL database, real pgvector cosine, and the
real ``CognitiveMemoryRecallService`` (only the embedding provider is
faked): the historical current-truth predicate (a presently SUPERSEDED
item that WAS current truth at an earlier ``as_of`` is still returned, with
``is_current_truth=true`` -- never a naive ``lifecycle = 'active'``
filter), FORGOTTEN's absolute exclusion even with a NULL embedding, exact
cosine ranking with deterministic tie-breaks, scope isolation, and the
embedding-before-PostgreSQL-session ordering.

SM-1004 (Supersede/Forget) is not implemented yet, so SUPERSEDED/FORGOTTEN
fixtures are inserted directly via the ORM, exactly as ADR-0016's own
schema already allows and as the SM-1003 backlog anticipates. Requires
migrations already applied through 0018.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    MemoryItem,
    MemoryProvenance,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.memories import MemoryRecallRequest
from sofias_memory.services.cognitive_memory_recall import CognitiveMemoryRecallService
from tests.unit._app_factory import create_app

POSTGRES_COGNITIVE_MEMORY_RECALL_ENV = "SOFIAS_MEMORY_RUN_COGNITIVE_MEMORY_RECALL_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
EMBEDDING_DIMENSIONS = 3072
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def _one_hot(index: int, *, magnitude: float = 1.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = magnitude
    return vector


VEC_A = _one_hot(0)
"""cosine(VEC_A, VEC_A) = 1.0."""

VEC_B = [0.0] * EMBEDDING_DIMENSIONS
VEC_B[0] = 0.5
VEC_B[1] = 0.5
"""cosine(VEC_A, VEC_B) ~= 0.7071 -- partial alignment, still positive."""

VEC_C = _one_hot(2)
"""cosine(VEC_A, VEC_C) = 0.0 -- orthogonal, irrelevant to a VEC_A query."""

QUERY_TEXT = "typed recall integration query"


class DeterministicEmbeddingClient:
    """Maps exact input text to a pre-registered vector -- lets a test
    control precisely what the recall query embedding will be. Raises
    loudly on an unregistered text so a test never silently ranks against
    the wrong vector (same pattern as the Skills resolve integration
    suite)."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self._vectors = dict(vectors or {})
        self.calls: list[str] = []

    def register(self, text: str, vector: list[float]) -> None:
        self._vectors[text] = vector

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for text_value in texts:
            self.calls.append(text_value)
            if text_value not in self._vectors:
                raise AssertionError(f"unregistered embedding text: {text_value!r}")
            result.append(self._vectors[text_value])
        return result


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


class BlockingEmbeddingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [list(VEC_A) for _ in texts]


def _recall_test_database_url(env: dict[str, str]) -> None:
    if env.get(POSTGRES_COGNITIVE_MEMORY_RECALL_ENV) != "1":
        pytest.skip(f"set {POSTGRES_COGNITIVE_MEMORY_RECALL_ENV}=1 to run this suite")


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _recall_test_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup(session_factory: AsyncSessionFactory, memory_ids: set[UUID]) -> None:
    if not memory_ids:
        return
    async with session_factory() as session:
        await session.execute(
            delete(CognitiveMemoryIdempotency).where(
                CognitiveMemoryIdempotency.target_memory_id.in_(memory_ids)
                | CognitiveMemoryIdempotency.result_memory_id.in_(memory_ids)
            )
        )
        await session.execute(delete(MemoryItem).where(MemoryItem.superseded_by.in_(memory_ids)))
        await session.execute(delete(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
        await session.commit()


def unique_scope(prefix: str) -> str:
    return f"project:{prefix}-{uuid4().hex[:16]}"


async def insert_active_item(
    session_factory: AsyncSessionFactory,
    *,
    scope: str,
    embedding: list[float] | None,
    created_at: datetime,
    memory_type: str = "profile",
    content: str = "fixture content",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    confidence: float | None = None,
) -> UUID:
    memory_id = uuid4()
    async with session_factory() as session:
        session.add(
            MemoryItem(
                id=memory_id,
                memory_type=memory_type,
                scope=scope,
                content=content,
                embedding=embedding,
                lifecycle="active",
                confidence=confidence,
                valid_from=valid_from,
                valid_until=valid_until,
                created_at=created_at,
            )
        )
        await session.flush()
        session.add(
            MemoryProvenance(
                memory_id=memory_id,
                origin_kind="user_asserted",
                source_system="sofias-assistant",
                turn_uuid=uuid4(),
            )
        )
        await session.commit()
    return memory_id


async def insert_superseded_pair(
    session_factory: AsyncSessionFactory,
    *,
    scope: str,
    old_embedding: list[float] | None,
    replacement_embedding: list[float] | None,
    old_created_at: datetime,
    superseded_at: datetime,
    old_valid_from: datetime | None = None,
    old_valid_until: datetime | None = None,
) -> tuple[UUID, UUID]:
    """Old item: lifecycle=superseded, superseded_at/superseded_by set.
    Replacement: lifecycle=active, created_at approx at superseded_at.
    Inserted replacement-first because ``superseded_by`` is an immediate
    (non-deferred) FK to ``memory_items.id``."""

    old_id = uuid4()
    replacement_id = uuid4()
    async with session_factory() as session:
        session.add(
            MemoryItem(
                id=replacement_id,
                memory_type="profile",
                scope=scope,
                content="replacement content",
                embedding=replacement_embedding,
                lifecycle="active",
                created_at=superseded_at,
            )
        )
        await session.flush()
        session.add(
            MemoryProvenance(
                memory_id=replacement_id,
                origin_kind="user_asserted",
                source_system="sofias-assistant",
                turn_uuid=uuid4(),
            )
        )
        session.add(
            MemoryItem(
                id=old_id,
                memory_type="profile",
                scope=scope,
                content="old content",
                embedding=old_embedding,
                lifecycle="superseded",
                created_at=old_created_at,
                valid_from=old_valid_from,
                valid_until=old_valid_until,
                superseded_at=superseded_at,
                superseded_by=replacement_id,
            )
        )
        await session.flush()
        session.add(
            MemoryProvenance(
                memory_id=old_id,
                origin_kind="user_asserted",
                source_system="sofias-assistant",
                turn_uuid=uuid4(),
            )
        )
        await session.commit()
    return old_id, replacement_id


async def insert_forgotten_item(
    session_factory: AsyncSessionFactory,
    *,
    created_at: datetime,
    forgotten_at: datetime,
) -> UUID:
    """Tombstone shape approved by ADR-0016 SS 13: every cognitive-content
    column NULL, ``forgotten_at`` set. Provenance is scrubbed to
    ``origin_kind``/``source_system`` only."""

    memory_id = uuid4()
    async with session_factory() as session:
        session.add(
            MemoryItem(
                id=memory_id,
                memory_type="profile",
                scope=None,
                content=None,
                embedding=None,
                lifecycle="forgotten",
                confidence=None,
                valid_from=None,
                valid_until=None,
                created_at=created_at,
                forgotten_at=forgotten_at,
            )
        )
        await session.flush()
        session.add(
            MemoryProvenance(
                memory_id=memory_id,
                origin_kind="user_asserted",
                source_system="sofias-assistant",
            )
        )
        await session.commit()
    return memory_id


def recall_service(
    session_factory: AsyncSessionFactory, embedding_client: object
) -> CognitiveMemoryRecallService:
    return CognitiveMemoryRecallService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


def build_recall_request(**overrides: object) -> MemoryRecallRequest:
    base: dict[str, object] = {
        "query": QUERY_TEXT,
        "scopes": ["global"],
        "top_k": 10,
    }
    base.update(overrides)
    return MemoryRecallRequest(**base)  # type: ignore[arg-type]


def test_recall_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _recall_test_database_url({})


# --- historical current-truth (acceptance criterion) -------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_currently_superseded_item_returned_as_current_truth_before_supersession(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """The mandatory acceptance scenario: an item now SUPERSEDED must still
    be returned as current truth for an ``as_of`` strictly before its
    ``superseded_at`` -- proving the implementation never applies a naive
    ``lifecycle = 'active'`` filter."""

    scope = unique_scope("historical")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 10, tzinfo=UTC)
    t1 = datetime(2026, 1, 5, tzinfo=UTC)  # t0 < t1 < t2

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_C,  # orthogonal: irrelevant to the query
        old_created_at=t0,
        superseded_at=t2,
    )
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=t1, include_superseded=False)
        )
        matches = [item for item in result.items if item.memory.memory_id == old_id]
        assert len(matches) == 1
        assert matches[0].is_current_truth is True
        assert all(item.memory.memory_id != replacement_id for item in result.items)
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_item_excluded_after_supersession_when_include_superseded_false(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    scope = unique_scope("after-super-excluded")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 10, tzinfo=UTC)
    t3 = datetime(2026, 1, 15, tzinfo=UTC)  # after superseded_at

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_A,
        old_created_at=t0,
        superseded_at=t2,
    )
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=t3, include_superseded=False)
        )
        returned_ids = {item.memory.memory_id for item in result.items}
        assert old_id not in returned_ids
        assert replacement_id in returned_ids
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_item_included_with_is_current_truth_false_when_flag_true(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    scope = unique_scope("historical-flagged")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 10, tzinfo=UTC)
    t3 = datetime(2026, 1, 15, tzinfo=UTC)

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_A,
        old_created_at=t0,
        superseded_at=t2,
    )
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=t3, include_superseded=True, top_k=50)
        )
        by_id = {item.memory.memory_id: item for item in result.items}
        assert old_id in by_id
        assert by_id[old_id].is_current_truth is False
        assert replacement_id in by_id
        assert by_id[replacement_id].is_current_truth is True
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


# --- include_superseded=true still enforces temporal validity ----------------
#
# External-review finding (post-SM-1003-closeout): a historical superseded
# item must remain subject to its own valid_from/valid_until window at
# as_of even when include_superseded=true -- existence + not-yet-forgotten
# alone is not enough (Feature Contract SS 14.1). Cases A-D below are the
# exact scenarios from that finding.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_a_historical_superseded_before_valid_from_excluded(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Case A: item not yet valid at as_of -- excluded even though it
    existed (created_at <= as_of) and was not yet superseded."""

    scope = unique_scope("case-a-before-valid-from")
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    valid_from = datetime(2026, 1, 20, tzinfo=UTC)
    superseded_at = datetime(2026, 2, 10, tzinfo=UTC)
    as_of = datetime(2026, 1, 10, tzinfo=UTC)  # before valid_from
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_C,
        old_created_at=created_at,
        superseded_at=superseded_at,
        old_valid_from=valid_from,
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=as_of, include_superseded=True, top_k=50)
        )
        assert old_id not in {item.memory.memory_id for item in result.items}
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_b_historical_superseded_at_valid_from_included(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Case B: valid_from is inclusive -- eligible exactly at as_of == valid_from."""

    scope = unique_scope("case-b-at-valid-from")
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    valid_from = datetime(2026, 1, 20, tzinfo=UTC)
    superseded_at = datetime(2026, 2, 10, tzinfo=UTC)
    as_of = valid_from
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_C,
        old_created_at=created_at,
        superseded_at=superseded_at,
        old_valid_from=valid_from,
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=as_of, include_superseded=True, top_k=50)
        )
        assert old_id in {item.memory.memory_id for item in result.items}
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_c_historical_superseded_at_valid_until_excluded(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Case C: valid_until is exclusive -- not eligible exactly at
    as_of == valid_until."""

    scope = unique_scope("case-c-at-valid-until")
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    valid_until = datetime(2026, 1, 30, tzinfo=UTC)
    superseded_at = datetime(2026, 2, 10, tzinfo=UTC)
    as_of = valid_until
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_C,
        old_created_at=created_at,
        superseded_at=superseded_at,
        old_valid_until=valid_until,
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=as_of, include_superseded=True, top_k=50)
        )
        assert old_id not in {item.memory.memory_id for item in result.items}
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_d_historical_superseded_after_supersession_but_temporally_valid(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Case D: as_of is after superseded_at (so the item is not current
    truth) but still inside its own valid_from/valid_until window --
    eligible for include_superseded=true, with is_current_truth=false."""

    scope = unique_scope("case-d-after-super-still-valid")
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    valid_until = datetime(2026, 3, 1, tzinfo=UTC)
    superseded_at = datetime(2026, 2, 1, tzinfo=UTC)
    as_of = datetime(2026, 2, 15, tzinfo=UTC)  # after superseded_at, before valid_until
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    old_id, replacement_id = await insert_superseded_pair(
        postgres_session_factory,
        scope=scope,
        old_embedding=VEC_A,
        replacement_embedding=VEC_C,
        old_created_at=created_at,
        superseded_at=superseded_at,
        old_valid_from=valid_from,
        old_valid_until=valid_until,
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=as_of, include_superseded=True, top_k=50)
        )
        by_id = {item.memory.memory_id: item for item in result.items}
        assert old_id in by_id
        assert by_id[old_id].is_current_truth is False
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


# --- FORGOTTEN absolute exclusion (acceptance criterion) ----------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forgotten_tombstone_never_returned(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """A FORGOTTEN row is structurally scope=NULL/content=NULL/
    embedding=NULL (ADR-0016 SS 13's tombstone shape), so it can never
    satisfy any caller-supplied scope filter on its own -- but a query that
    scans a table containing such a row, alongside a normal ACTIVE sibling
    in the queried scope, must still complete without erroring on the NULL
    embedding, and must never return the tombstone under any
    ``include_superseded``/``as_of`` combination."""

    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    forgotten_at = datetime(2026, 1, 5, tzinfo=UTC)
    before_forgotten = datetime(2026, 1, 3, tzinfo=UTC)
    now = datetime(2026, 1, 20, tzinfo=UTC)
    scope = unique_scope("forgotten")

    memory_id = await insert_forgotten_item(
        postgres_session_factory, created_at=created_at, forgotten_at=forgotten_at
    )
    sibling_id = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    try:
        for include_superseded in (False, True):
            for as_of in (now, before_forgotten):
                result = await recall_service(postgres_session_factory, client).recall(
                    build_recall_request(
                        scopes=[scope],
                        as_of=as_of,
                        include_superseded=include_superseded,
                        top_k=50,
                    )
                )
                returned_ids = {item.memory.memory_id for item in result.items}
                assert memory_id not in returned_ids
                assert sibling_id in returned_ids
    finally:
        await cleanup(postgres_session_factory, {memory_id, sibling_id})


# --- validity window boundaries -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validity_window_boundaries(postgres_session_factory: AsyncSessionFactory) -> None:
    created_at = datetime(2025, 1, 1, tzinfo=UTC)
    valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    valid_until = datetime(2026, 2, 1, tzinfo=UTC)
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    scope = unique_scope("validity")
    memory_id = await insert_active_item(
        postgres_session_factory,
        scope=scope,
        embedding=VEC_A,
        created_at=created_at,
        valid_from=valid_from,
        valid_until=valid_until,
    )

    async def eligible_at(as_of: datetime) -> bool:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], as_of=as_of, top_k=50)
        )
        return memory_id in {item.memory.memory_id for item in result.items}

    try:
        assert await eligible_at(valid_from - timedelta(seconds=1)) is False
        assert await eligible_at(valid_from) is True
        assert await eligible_at(valid_until - timedelta(microseconds=1)) is True
        assert await eligible_at(valid_until) is False
    finally:
        await cleanup(postgres_session_factory, {memory_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_created_after_as_of_is_always_excluded(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created_at = datetime(2026, 6, 1, tzinfo=UTC)
    as_of = datetime(2026, 5, 1, tzinfo=UTC)  # strictly before created_at
    scope = unique_scope("future-created")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    memory_id = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    try:
        for include_superseded in (False, True):
            result = await recall_service(postgres_session_factory, client).recall(
                build_recall_request(
                    scopes=[scope], as_of=as_of, include_superseded=include_superseded, top_k=50
                )
            )
            assert memory_id not in {item.memory.memory_id for item in result.items}
    finally:
        await cleanup(postgres_session_factory, {memory_id})


# --- exact cosine ranking + deterministic ties --------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ranking_is_exact_cosine_descending(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("ranking")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_a = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    id_b = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_B, created_at=created_at
    )
    id_c = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_C, created_at=created_at
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=50)
        )
        ordered_ids = [item.memory.memory_id for item in result.items]
        assert ordered_ids.index(id_a) < ordered_ids.index(id_b) < ordered_ids.index(id_c)
        by_id = {item.memory.memory_id: item.relevance for item in result.items}
        assert by_id[id_a] == pytest.approx(1.0, abs=1e-6)
        assert by_id[id_b] == pytest.approx(0.7071, abs=1e-3)
        assert by_id[id_c] == pytest.approx(0.0, abs=1e-6)
    finally:
        await cleanup(postgres_session_factory, {id_a, id_b, id_c})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_min_relevance_boundary_is_inclusive(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("min-relevance")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_a = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    id_c = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_C, created_at=created_at
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=50, min_relevance=0.999)
        )
        returned_ids = {item.memory.memory_id for item in result.items}
        assert id_a in returned_ids
        assert id_c not in returned_ids

        exact_boundary = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=50, min_relevance=0.0)
        )
        exact_ids = {item.memory.memory_id for item in exact_boundary.items}
        assert id_c in exact_ids  # cosine(VEC_A, VEC_C) == 0.0 -- boundary is inclusive
    finally:
        await cleanup(postgres_session_factory, {id_a, id_c})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_relevance_ties_break_by_created_at_desc_then_id_asc(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, tzinfo=UTC)
    scope = unique_scope("ties")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_old = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=older
    )
    id_new = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=newer
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=50)
        )
        ordered_ids = [item.memory.memory_id for item in result.items]
        assert ordered_ids.index(id_new) < ordered_ids.index(id_old)

        tied_partner = await insert_active_item(
            postgres_session_factory, scope=scope, embedding=VEC_A, created_at=older
        )
        try:
            tie_result = await recall_service(postgres_session_factory, client).recall(
                build_recall_request(scopes=[scope], top_k=50)
            )
            tied_ids = {id_old, tied_partner}
            tie_ordered = [
                item.memory.memory_id
                for item in tie_result.items
                if item.memory.memory_id in tied_ids
            ]
            assert tie_ordered == sorted(tied_ids, key=str)
        finally:
            await cleanup(postgres_session_factory, {tied_partner})
    finally:
        await cleanup(postgres_session_factory, {id_old, id_new})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confidence_does_not_affect_ranking(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("confidence-neutral")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_low_confidence = await insert_active_item(
        postgres_session_factory,
        scope=scope,
        embedding=VEC_A,
        created_at=created_at,
        confidence=0.01,
    )
    id_high_confidence = await insert_active_item(
        postgres_session_factory,
        scope=scope,
        embedding=VEC_A,
        created_at=created_at,
        confidence=0.99,
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=50)
        )
        by_id = {item.memory.memory_id: item.relevance for item in result.items}
        assert by_id[id_low_confidence] == by_id[id_high_confidence]
    finally:
        await cleanup(postgres_session_factory, {id_low_confidence, id_high_confidence})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_top_k_applied_after_ordering(postgres_session_factory: AsyncSessionFactory) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("top-k")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_a = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    id_b = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_B, created_at=created_at
    )
    id_c = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_C, created_at=created_at
    )

    try:
        result = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], top_k=1)
        )
        assert [item.memory.memory_id for item in result.items] == [id_a]
    finally:
        await cleanup(postgres_session_factory, {id_a, id_b, id_c})


# --- scope isolation -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scope_isolation(postgres_session_factory: AsyncSessionFactory) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    global_scope = "global"
    alpha_scope = unique_scope("alpha")
    beta_scope = unique_scope("beta")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_global = await insert_active_item(
        postgres_session_factory, scope=global_scope, embedding=VEC_A, created_at=created_at
    )
    id_alpha = await insert_active_item(
        postgres_session_factory, scope=alpha_scope, embedding=VEC_A, created_at=created_at
    )
    id_beta = await insert_active_item(
        postgres_session_factory, scope=beta_scope, embedding=VEC_A, created_at=created_at
    )

    try:
        alpha_only = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[alpha_scope], top_k=50)
        )
        alpha_only_ids = {item.memory.memory_id for item in alpha_only.items}
        assert alpha_only_ids == {id_alpha}

        global_plus_alpha = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[global_scope, alpha_scope], top_k=50)
        )
        combined_ids = {item.memory.memory_id for item in global_plus_alpha.items}
        assert id_global in combined_ids
        assert id_alpha in combined_ids
        assert id_beta not in combined_ids
    finally:
        await cleanup(postgres_session_factory, {id_global, id_alpha, id_beta})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_type_filtering(postgres_session_factory: AsyncSessionFactory) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("type-filter")
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})

    id_profile = await insert_active_item(
        postgres_session_factory,
        scope=scope,
        embedding=VEC_A,
        created_at=created_at,
        memory_type="profile",
    )
    id_semantic = await insert_active_item(
        postgres_session_factory,
        scope=scope,
        embedding=VEC_A,
        created_at=created_at,
        memory_type="semantic",
    )

    try:
        profile_only = await recall_service(postgres_session_factory, client).recall(
            build_recall_request(scopes=[scope], memory_types=["profile"], top_k=50)
        )
        returned_ids = {item.memory.memory_id for item in profile_only.items}
        assert id_profile in returned_ids
        assert id_semantic not in returned_ids
    finally:
        await cleanup(postgres_session_factory, {id_profile, id_semantic})


# --- embedding-before-transaction ordering + provider failure -----------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_call_holds_no_session_before_transaction(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session_factory_calls = 0
    real_factory = postgres_session_factory

    def counting_session_factory() -> Any:
        nonlocal session_factory_calls
        session_factory_calls += 1
        return real_factory()

    blocking = BlockingEmbeddingClient()
    service = CognitiveMemoryRecallService(
        load_settings(),
        embedding_client=blocking,
        session_factory=counting_session_factory,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(service.recall(build_recall_request()))
    try:
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        assert session_factory_calls == 0

        blocking.release.set()
        await asyncio.wait_for(task, timeout=5)
        assert session_factory_calls >= 1
    finally:
        blocking.release.set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failure_never_reaches_database(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session_factory_calls = 0
    real_factory = postgres_session_factory

    def counting_session_factory() -> Any:
        nonlocal session_factory_calls
        session_factory_calls += 1
        return real_factory()

    service = CognitiveMemoryRecallService(
        load_settings(),
        embedding_client=FailingEmbeddingClient(),
        session_factory=counting_session_factory,  # type: ignore[arg-type]
    )

    with pytest.raises(DependencyUnavailableError):
        await service.recall(build_recall_request())
    assert session_factory_calls == 0


# --- HTTP-level smoke ------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        llm_api_key=LLM_API_KEY,
        cognitive_idempotency_hmac_key=COGNITIVE_IDEMPOTENCY_HMAC_KEY,
        app_env="test",
    )


def build_app(
    session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding_client_factory: Any,
) -> Any:
    monkeypatch.setattr(
        "sofias_memory.api.routes.memories.OpenAIEmbeddingClient",
        embedding_client_factory,
    )
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_http_returns_200_with_typed_results(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    scope = unique_scope("http-smoke")
    memory_id = await insert_active_item(
        postgres_session_factory, scope=scope, embedding=VEC_A, created_at=created_at
    )
    client = DeterministicEmbeddingClient({QUERY_TEXT: VEC_A})
    app = build_app(
        postgres_session_factory, monkeypatch, embedding_client_factory=lambda settings: client
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http_client:
            response = await http_client.post(
                "/api/v1/memories/recall",
                headers=HEADERS,
                json={"query": QUERY_TEXT, "scopes": [scope]},
            )
        assert response.status_code == 200
        body = response.json()
        items = body["data"]["items"]
        assert len(items) == 1
        assert items[0]["memory"]["memory_id"] == str(memory_id)
        assert "embedding" not in items[0]["memory"]
        assert items[0]["is_current_truth"] is True
    finally:
        await cleanup(postgres_session_factory, {memory_id})
