from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.models import Feedback, Query
from sofias_memory.schemas.feedback import FeedbackRequest, FeedbackResult
from sofias_memory.services.feedback import (
    FeedbackService,
    FeedbackUnitOfWork,
    UnitOfWorkFactory,
)
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


class FakeStore:
    def __init__(self) -> None:
        self.queries: list[Query] = []
        self.feedback: list[Feedback] = []
        self.commits = 0


class FakeQueryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, query_id: UUID) -> Query | None:
        return next((query for query in self._store.queries if query.id == query_id), None)


class FakeFeedbackRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, feedback: Feedback) -> Feedback:
        self._store.feedback.append(feedback)
        return feedback

    async def get_by_id(self, feedback_id: UUID) -> Feedback | None:
        return next(
            (feedback for feedback in self._store.feedback if feedback.id == feedback_id),
            None,
        )


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.queries = FakeQueryRepository(store)
        self.feedback = FakeFeedbackRepository(store)
        self._store = store

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> FeedbackService:
    def create_uow() -> FeedbackUnitOfWork:
        return cast(FeedbackUnitOfWork, FakeUnitOfWork(store))

    return FeedbackService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def query_with_references(*chunk_ids: UUID) -> Query:
    return Query(
        id=uuid4(),
        query_text="What is Sofias Memory?",
        dataset_ids=[uuid4()],
        mode="rag",
        answer="Grounded answer.",
        references={
            "items": [
                {
                    "source_id": str(uuid4()),
                    "document_id": str(uuid4()),
                    "chunk_id": str(chunk_id),
                    "chunk_ordinal": ordinal,
                    "score": 0.03,
                }
                for ordinal, chunk_id in enumerate(chunk_ids)
            ]
        },
        timings={"embedding": 1, "retrieval": 1, "graph": 0, "generation": 1, "total": 3},
        model="gpt-test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_answer_feedback_persists_with_applied_at_none() -> None:
    store = FakeStore()
    query = query_with_references(uuid4())
    store.queries.append(query)

    result = await service_for(store).record(
        FeedbackRequest(
            query_id=query.id,
            target_type="answer",
            score=1,
            comment="  resposta correta  ",
        )
    )

    assert result.query_id == query.id
    assert result.target_type == "answer"
    assert result.target_id is None
    assert result.score == 1
    assert result.comment == "resposta correta"
    assert result.applied_at is None
    assert result.created_at.tzinfo is not None
    assert store.feedback[0].applied_at is None
    assert store.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [-1, 0])
async def test_negative_and_neutral_scores_are_accepted(score: int) -> None:
    store = FakeStore()
    query = query_with_references()
    store.queries.append(query)

    result = await service_for(store).record(
        FeedbackRequest(query_id=query.id, target_type="answer", score=cast(object, score))
    )

    assert result.score == score


@pytest.mark.asyncio
async def test_missing_query_returns_404() -> None:
    store = FakeStore()

    with pytest.raises(SofiasMemoryError) as exc_info:
        await service_for(store).record(
            FeedbackRequest(query_id=uuid4(), target_type="answer", score=1)
        )

    assert exc_info.value.status_code == 404
    assert store.feedback == []
    assert store.commits == 0


@pytest.mark.asyncio
async def test_answer_feedback_rejects_target_id_even_if_constructed_directly() -> None:
    store = FakeStore()
    query = query_with_references()
    store.queries.append(query)
    request = FeedbackRequest.model_construct(
        query_id=query.id,
        target_type="answer",
        target_id=uuid4(),
        score=1,
        comment=None,
    )

    with pytest.raises(SofiasMemoryError, match="Answer feedback"):
        await service_for(store).record(request)


@pytest.mark.asyncio
async def test_reference_feedback_requires_returned_chunk_id() -> None:
    target_chunk_id = uuid4()
    store = FakeStore()
    query = query_with_references(target_chunk_id)
    store.queries.append(query)

    result = await service_for(store).record(
        FeedbackRequest(
            query_id=query.id,
            target_type="reference",
            target_id=target_chunk_id,
            score=-1,
            comment="bad reference",
        )
    )

    assert result.target_type == "reference"
    assert result.target_id == target_chunk_id
    assert result.score == -1
    assert store.feedback[0].target_id == target_chunk_id


@pytest.mark.asyncio
async def test_reference_feedback_rejects_missing_or_unknown_reference_target() -> None:
    target_chunk_id = uuid4()
    store = FakeStore()
    query = query_with_references(target_chunk_id)
    query_without_references = query_with_references()
    store.queries.extend([query, query_without_references])

    missing_target = FeedbackRequest.model_construct(
        query_id=query.id,
        target_type="reference",
        target_id=None,
        score=1,
        comment=None,
    )
    with pytest.raises(SofiasMemoryError, match="requires target_id"):
        await service_for(store).record(missing_target)

    with pytest.raises(SofiasMemoryError, match="not returned"):
        await service_for(store).record(
            FeedbackRequest(
                query_id=query.id,
                target_type="reference",
                target_id=uuid4(),
                score=1,
            )
        )
    with pytest.raises(SofiasMemoryError, match="not returned"):
        await service_for(store).record(
            FeedbackRequest(
                query_id=query_without_references.id,
                target_type="reference",
                target_id=target_chunk_id,
                score=1,
            )
        )

    assert store.feedback == []


def test_feedback_schema_rejects_invalid_score_target_type_and_comment() -> None:
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id=uuid4(), target_type="answer", score=2)
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id=uuid4(), target_type="chunk", score=1)
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id=uuid4(), target_type="answer", target_id=uuid4(), score=1)
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id=uuid4(), target_type="reference", score=1)
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id=uuid4(), target_type="answer", score=1, comment="x" * 4001)

    request = FeedbackRequest(query_id=uuid4(), target_type="answer", score=1, comment="   ")
    assert request.comment is None


@pytest.mark.asyncio
async def test_feedback_route_returns_envelope_and_requires_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_id = uuid4()
    feedback_id = uuid4()

    class FakeFeedbackService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def record(self, request: FeedbackRequest) -> FeedbackResult:
            assert request.query_id == query_id
            return FeedbackResult(
                feedback_id=feedback_id,
                query_id=request.query_id,
                target_type=request.target_type,
                target_id=request.target_id,
                score=request.score,
                comment=request.comment,
                applied_at=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

    monkeypatch.setattr("sofias_memory.api.routes.feedback.FeedbackService", FakeFeedbackService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key_response = await client.post(
            "/api/v1/feedback",
            json={"query_id": str(query_id), "target_type": "answer", "score": 1},
        )
        response = await client.post(
            "/api/v1/feedback",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={
                "query_id": str(query_id),
                "target_type": "answer",
                "target_id": None,
                "score": 1,
                "comment": " ok ",
            },
        )

    assert missing_key_response.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["feedback_id"] == str(feedback_id)
    assert body["data"]["query_id"] == str(query_id)
    assert body["data"]["target_type"] == "answer"
    assert body["data"]["target_id"] is None
    assert body["data"]["comment"] == "ok"
    assert body["data"]["applied_at"] is None
    assert body["meta"]["request_id"]
