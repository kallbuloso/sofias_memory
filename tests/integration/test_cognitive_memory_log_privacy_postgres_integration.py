"""Real-PostgreSQL log-privacy hardening for Cognitive Memory (SM-1005,
ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory).

Proves, by driving the real HTTP surface end to end and inspecting the
actual rendered structlog/JSON output, that no code path in Create,
Supersede, Forget, or Typed Recall -- success or error -- ever logs: full
cognitive content, an external provenance reference, an embedding/query
vector, the ``COGNITIVE_IDEMPOTENCY_HMAC_KEY`` secret, a stored request
digest, or a full ``Idempotency-Key``. Every sentinel below is unique per
test run (``uuid4().hex``-suffixed) so a positive match cannot be an
unrelated coincidence.

``sofias_memory.services.cognitive_memory``/``cognitive_memory_recall``
never call ``get_logger()`` at all today; this suite exists so that if a
future change adds logging there (or in the routes module), an accidental
content/secret leak fails a test immediately instead of shipping.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from io import StringIO
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import CognitiveMemoryIdempotency, MemoryItem
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.observability.logging import clear_log_context, configure_logging
from tests.unit._app_factory import create_app

POSTGRES_COGNITIVE_MEMORY_HTTP_ENV = "SOFIAS_MEMORY_RUN_COGNITIVE_MEMORY_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
LLM_API_KEY = "sk-fake-test-key"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
EMBEDDING_DIMENSIONS = 3072
EMBEDDING_SENTINEL_VALUE = 0.421875
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[EMBEDDING_SENTINEL_VALUE] * EMBEDDING_DIMENSIONS for _ in texts]


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


def _cognitive_memory_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_COGNITIVE_MEMORY_HTTP_ENV) != "1":
        pytest.skip(f"set {POSTGRES_COGNITIVE_MEMORY_HTTP_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    import os

    _cognitive_memory_http_test_database_url(dict(os.environ))
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


async def fetch_request_digest(session_factory: AsyncSessionFactory, key: str) -> str:
    async with session_factory() as session:
        digest = await session.scalar(
            select(CognitiveMemoryIdempotency.request_digest).where(
                CognitiveMemoryIdempotency.idempotency_key == key
            )
        )
    assert digest is not None
    return str(digest)


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password=NEO4J_PASSWORD_FALLBACK,
        llm_api_key=LLM_API_KEY,
        cognitive_idempotency_hmac_key=COGNITIVE_IDEMPOTENCY_HMAC_KEY,
        app_env="test",
    )


def build_app(
    session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding_client_factory: Any = lambda settings: FakeEmbeddingClient(),
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


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def read_log_text(stream: StringIO) -> str:
    lines = [line for line in stream.getvalue().splitlines() if line]
    for line in lines:
        json.loads(line)  # every captured line must be well-formed JSON
    return stream.getvalue()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cognitive_memory_never_logs_content_provenance_vectors_or_secrets(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_sentinel = f"SENTINEL-CONTENT-{uuid4().hex}"
    replacement_content_sentinel = f"SENTINEL-REPLACEMENT-CONTENT-{uuid4().hex}"
    conflicting_content_sentinel = f"SENTINEL-CONFLICT-CONTENT-{uuid4().hex}"
    external_ref_sentinel = f"SENTINEL-EXTERNAL-REF-{uuid4().hex}"
    query_sentinel = f"SENTINEL-QUERY-{uuid4().hex}"
    create_key = f"SENTINEL-IDEMPOTENCY-KEY-CREATE-{uuid4().hex}"
    supersede_key = f"SENTINEL-IDEMPOTENCY-KEY-SUPERSEDE-{uuid4().hex}"
    forget_key = f"SENTINEL-IDEMPOTENCY-KEY-FORGET-{uuid4().hex}"

    app = build_app(postgres_session_factory, monkeypatch)
    memory_ids: set[UUID] = set()

    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    try:
        async with build_client(app) as client:
            # --- Create: success ---------------------------------------------
            create_response = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "profile",
                    "scope": "global",
                    "content": content_sentinel,
                    "provenance": {
                        "origin_kind": "imported",
                        "source_system": "ext-system",
                        "source_ref": external_ref_sentinel,
                        "confirmation_ref": external_ref_sentinel,
                    },
                },
                headers={**HEADERS, "Idempotency-Key": create_key},
            )
            assert create_response.status_code == 201
            memory_id = UUID(create_response.json()["data"]["memory_id"])
            memory_ids.add(memory_id)

            # --- Create: idempotency conflict (error path) --------------------
            conflict_response = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "profile",
                    "scope": "global",
                    "content": conflicting_content_sentinel,
                    "provenance": {
                        "origin_kind": "imported",
                        "source_system": "ext-system",
                        "source_ref": external_ref_sentinel,
                    },
                },
                headers={**HEADERS, "Idempotency-Key": create_key},
            )
            assert conflict_response.status_code == 409

            # --- Supersede: success --------------------------------------------
            supersede_response = await client.post(
                f"/api/v1/memories/{memory_id}/supersede",
                json={
                    "content": replacement_content_sentinel,
                    "provenance": {
                        "origin_kind": "imported",
                        "source_system": "ext-system",
                        "source_ref": external_ref_sentinel,
                    },
                },
                headers={**HEADERS, "Idempotency-Key": supersede_key},
            )
            assert supersede_response.status_code == 200
            replacement_id = UUID(supersede_response.json()["data"]["replacement"]["memory_id"])
            memory_ids.add(replacement_id)

            # --- Supersede: not-found (error path) ------------------------------
            missing_supersede_response = await client.post(
                f"/api/v1/memories/{uuid4()}/supersede",
                json={
                    "content": conflicting_content_sentinel,
                    "provenance": {
                        "origin_kind": "imported",
                        "source_system": "ext-system",
                        "source_ref": external_ref_sentinel,
                    },
                },
                headers=HEADERS,
            )
            assert missing_supersede_response.status_code == 404

            # --- Forget: success -------------------------------------------------
            forget_response = await client.post(
                f"/api/v1/memories/{replacement_id}/forget",
                headers={**HEADERS, "Idempotency-Key": forget_key},
            )
            assert forget_response.status_code == 200

            # --- Forget: not-found (error path) -----------------------------------
            missing_forget_response = await client.post(
                f"/api/v1/memories/{uuid4()}/forget",
                headers=HEADERS,
            )
            assert missing_forget_response.status_code == 404

            # --- Recall: provider failure (error path) -----------------------------
            monkeypatch.setattr(
                "sofias_memory.api.routes.memories.OpenAIEmbeddingClient",
                lambda settings: FailingEmbeddingClient(),
            )
            recall_response = await client.post(
                "/api/v1/memories/recall",
                json={"query": query_sentinel, "scopes": ["global"]},
                headers=HEADERS,
            )
            assert recall_response.status_code == 503
        create_digest = await fetch_request_digest(postgres_session_factory, create_key)
    finally:
        clear_log_context()
        await cleanup(postgres_session_factory, memory_ids)

    log_text = read_log_text(stream)

    forbidden_values = {
        "full cognitive content (create)": content_sentinel,
        "full cognitive content (supersede replacement)": replacement_content_sentinel,
        "full cognitive content (conflicting/rejected request)": conflicting_content_sentinel,
        "external provenance reference": external_ref_sentinel,
        "recall query text": query_sentinel,
        "COGNITIVE_IDEMPOTENCY_HMAC_KEY secret": COGNITIVE_IDEMPOTENCY_HMAC_KEY,
        "stored request digest (create)": create_digest,
        "full Idempotency-Key (create)": create_key,
        "full Idempotency-Key (supersede)": supersede_key,
        "full Idempotency-Key (forget)": forget_key,
        "embedding/query vector value": repr(EMBEDDING_SENTINEL_VALUE),
    }
    leaked = {label: value for label, value in forbidden_values.items() if value in log_text}
    assert leaked == {}, f"log stream leaked: {sorted(leaked)}"


def test_cognitive_memory_log_privacy_suite_skips_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _cognitive_memory_http_test_database_url({})
