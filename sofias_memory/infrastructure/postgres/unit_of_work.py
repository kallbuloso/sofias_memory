"""Explicit PostgreSQL Unit of Work."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.repositories import (
    DatasetRepository,
    DocumentRepository,
    GraphOutboxRepository,
    PipelineRunRepository,
    PipelineStepRepository,
    SourceRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory


class PostgresUnitOfWork:
    """Own one AsyncSession and transaction boundary for a write operation."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False
        self._rolled_back = False
        self._datasets: DatasetRepository | None = None
        self._sources: SourceRepository | None = None
        self._documents: DocumentRepository | None = None
        self._pipeline_runs: PipelineRunRepository | None = None
        self._pipeline_steps: PipelineStepRepository | None = None
        self._graph_outbox: GraphOutboxRepository | None = None

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("PostgresUnitOfWork is already active")

        session = self._session_factory()
        self._session = session
        self._committed = False
        self._rolled_back = False
        self._datasets = DatasetRepository(session)
        self._sources = SourceRepository(session)
        self._documents = DocumentRepository(session)
        self._pipeline_runs = PipelineRunRepository(session)
        self._pipeline_steps = PipelineStepRepository(session)
        self._graph_outbox = GraphOutboxRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or (not self._committed and not self._rolled_back):
                await session.rollback()
        finally:
            await session.close()
            self._clear()

    @property
    def datasets(self) -> DatasetRepository:
        if self._datasets is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._datasets

    @property
    def sources(self) -> SourceRepository:
        if self._sources is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._sources

    @property
    def documents(self) -> DocumentRepository:
        if self._documents is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._documents

    @property
    def pipeline_runs(self) -> PipelineRunRepository:
        if self._pipeline_runs is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._pipeline_runs

    @property
    def pipeline_steps(self) -> PipelineStepRepository:
        if self._pipeline_steps is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._pipeline_steps

    @property
    def graph_outbox(self) -> GraphOutboxRepository:
        if self._graph_outbox is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._graph_outbox

    async def commit(self) -> None:
        session = self._require_session()
        await session.commit()
        self._committed = True
        self._rolled_back = False

    async def rollback(self) -> None:
        session = self._require_session()
        await session.rollback()
        self._rolled_back = True
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("PostgresUnitOfWork is not active")
        return self._session

    def _clear(self) -> None:
        self._session = None
        self._committed = False
        self._rolled_back = False
        self._datasets = None
        self._sources = None
        self._documents = None
        self._pipeline_runs = None
        self._pipeline_steps = None
        self._graph_outbox = None
