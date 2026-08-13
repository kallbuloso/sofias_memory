"""Durable feedback recording service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.infrastructure.postgres.models import Feedback, Query
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.feedback import FeedbackRequest, FeedbackResult

ANSWER_TARGET_TYPE = "answer"
REFERENCE_TARGET_TYPE = "reference"


class QueryRepositoryForFeedback(Protocol):
    async def get_by_id(self, query_id: UUID) -> Query | None: ...


class FeedbackRepositoryForFeedback(Protocol):
    async def add(self, feedback: Feedback) -> Feedback: ...
    async def get_by_id(self, feedback_id: UUID) -> Feedback | None: ...


class FeedbackUnitOfWork(Protocol):
    queries: QueryRepositoryForFeedback
    feedback: FeedbackRepositoryForFeedback

    async def __aenter__(self) -> FeedbackUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], FeedbackUnitOfWork]


class FeedbackService:
    """Record feedback without applying ranking or knowledge changes."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def record(self, request: FeedbackRequest) -> FeedbackResult:
        async with self._unit_of_work_factory() as uow:
            query = await uow.queries.get_by_id(request.query_id)
            if query is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Query does not exist.",
                    details={"query_id": str(request.query_id)},
                )
            validate_feedback_target(request, query)
            feedback = Feedback(
                id=uuid4(),
                query_id=request.query_id,
                target_type=request.target_type,
                target_id=request.target_id,
                score=request.score,
                comment=request.comment,
                applied_at=None,
                created_at=utc_now(),
            )
            feedback = await uow.feedback.add(feedback)
            await uow.commit()
            return result_from_feedback(feedback)


def validate_feedback_target(request: FeedbackRequest, query: Query) -> None:
    if request.target_type == ANSWER_TARGET_TYPE:
        if request.target_id is not None:
            raise invalid_target_error("Answer feedback must not include target_id.")
        return

    if request.target_type == REFERENCE_TARGET_TYPE:
        if request.target_id is None:
            raise invalid_target_error("Reference feedback requires target_id.")
        if request.target_id not in reference_chunk_ids(query.references):
            raise invalid_target_error("Reference target_id was not returned by the query.")
        return

    raise invalid_target_error("Feedback target type is not supported.")


def reference_chunk_ids(references: Mapping[str, object]) -> set[UUID]:
    items = references.get("items")
    if not isinstance(items, list):
        return set()
    chunk_ids: set[UUID] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw_chunk_id = item.get("chunk_id")
        if not isinstance(raw_chunk_id, str):
            continue
        try:
            chunk_ids.add(UUID(raw_chunk_id))
        except ValueError:
            continue
    return chunk_ids


def invalid_target_error(message: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.BAD_REQUEST,
        message=message,
    )


def result_from_feedback(feedback: Feedback) -> FeedbackResult:
    return FeedbackResult(
        feedback_id=feedback.id,
        query_id=feedback.query_id,
        target_type=feedback.target_type,
        target_id=feedback.target_id,
        score=feedback.score,
        comment=feedback.comment,
        applied_at=feedback.applied_at,
        created_at=feedback.created_at,
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> FeedbackUnitOfWork:
        return cast(FeedbackUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
