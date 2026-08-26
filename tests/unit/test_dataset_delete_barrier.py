"""Unit coverage for the shared ADR-0010 D12/D28 delete-intent barrier
helper (SM-515) -- fast, no PostgreSQL needed for the pure branch logic.
Real concurrency/ownership-predicate behavior is proven against real
PostgreSQL in tests/integration/test_dataset_delete_postgres_integration.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from sofias_memory.domain import DatasetStatus
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.dataset_delete_barrier import (
    raise_if_dataset_administratively_blocked,
)


@dataclass
class _FakeDataset:
    status: DatasetStatus


class _FakeDatasetRepository:
    def __init__(self, dataset: _FakeDataset | None) -> None:
        self._dataset = dataset

    async def get_by_id(self, dataset_id: object) -> _FakeDataset | None:
        del dataset_id
        return self._dataset


class _FakePipelineRunRepository:
    def __init__(self, *, nonterminal_exists: bool, administratively_owned: bool) -> None:
        self._nonterminal_exists = nonterminal_exists
        self._administratively_owned = administratively_owned

    async def find_nonterminal_dataset_delete_for_dataset(
        self, dataset_id: object
    ) -> object | None:
        del dataset_id
        return object() if self._nonterminal_exists else None

    async def exists_administrative_delete_ownership(self, dataset_id: object) -> bool:
        del dataset_id
        return self._administratively_owned


@dataclass
class _FakeUnitOfWork:
    datasets: _FakeDatasetRepository
    pipeline_runs: _FakePipelineRunRepository


@pytest.mark.asyncio
async def test_none_dataset_id_is_never_blocked() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(None),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=False
        ),
    )
    await raise_if_dataset_administratively_blocked(uow, None)


@pytest.mark.asyncio
async def test_missing_dataset_is_never_blocked_by_this_helper() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(None),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=False
        ),
    )
    await raise_if_dataset_administratively_blocked(uow, uuid4())


@pytest.mark.asyncio
async def test_active_no_nonterminal_no_ownership_is_not_blocked() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(_FakeDataset(status=DatasetStatus.ACTIVE)),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=False
        ),
    )
    await raise_if_dataset_administratively_blocked(uow, uuid4())


@pytest.mark.asyncio
async def test_deleted_dataset_raises_dataset_deleted_conflict() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(_FakeDataset(status=DatasetStatus.DELETED)),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=False
        ),
    )
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - SofiasMemoryError, checked below
        await raise_if_dataset_administratively_blocked(uow, uuid4())
    assert exc_info.value.code == ErrorCode.DATASET_DELETED  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_nonterminal_dataset_delete_raises_dataset_deleting_conflict() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(_FakeDataset(status=DatasetStatus.ACTIVE)),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=True, administratively_owned=False
        ),
    )
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await raise_if_dataset_administratively_blocked(uow, uuid4())
    assert exc_info.value.code == ErrorCode.DATASET_DELETING  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_administratively_owned_deleting_raises_dataset_deleting_conflict() -> None:
    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(_FakeDataset(status=DatasetStatus.DELETING)),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=True
        ),
    )
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await raise_if_dataset_administratively_blocked(uow, uuid4())
    assert exc_info.value.code == ErrorCode.DATASET_DELETING  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ordinary_forget_owned_deleting_is_not_blocked() -> None:
    """ADR-0010 D28's counterexample, at the barrier level: a DELETING
    dataset with no administrative ownership (ordinary Forget's own
    transient state) must not be blocked."""

    uow = _FakeUnitOfWork(
        datasets=_FakeDatasetRepository(_FakeDataset(status=DatasetStatus.DELETING)),
        pipeline_runs=_FakePipelineRunRepository(
            nonterminal_exists=False, administratively_owned=False
        ),
    )
    await raise_if_dataset_administratively_blocked(uow, uuid4())
