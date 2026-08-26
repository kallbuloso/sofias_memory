"""Shared ADR-0010 D12/D28 delete-intent barrier check (SM-515).

One PostgreSQL-authoritative helper, reused by every dataset-scoped write
submission path (``PipelineSubmissionService``'s own create-new-run
sequence, covering Remember/Cognify/Improve/Forget and manual retry alike
via ``submit_trusted_internal``) plus ``RunControlService.retry()``'s own
early D16 check -- never copied per route (ADR-0010 "Shared submission
integration": "Preferir uma pequena PostgreSQL policy/helper compartilhada
a copiar a mesma barrier em quatro routes").

Deliberately has no dependency on ``pipeline_submission.py`` or
``run_control.py``, so both can import it without a cycle.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Protocol
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import DatasetStatus
from sofias_memory.schemas.common import ErrorCode


class _DatasetForBarrier(Protocol):
    status: DatasetStatus


class DatasetRepositoryForBarrier(Protocol):
    async def get_by_id(self, dataset_id: UUID) -> _DatasetForBarrier | None: ...


class PipelineRunRepositoryForBarrier(Protocol):
    async def find_nonterminal_dataset_delete_for_dataset(
        self, dataset_id: UUID
    ) -> object | None: ...
    async def exists_administrative_delete_ownership(self, dataset_id: UUID) -> bool: ...


class UnitOfWorkForBarrier(Protocol):
    @property
    def datasets(self) -> DatasetRepositoryForBarrier: ...

    @property
    def pipeline_runs(self) -> PipelineRunRepositoryForBarrier: ...


def dataset_deleting_conflict_error(dataset_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.DATASET_DELETING,
        status_code=HTTPStatus.CONFLICT,
        message="This dataset is administratively deleting; new writes are not accepted.",
        details={"dataset_id": str(dataset_id)},
    )


def dataset_deleted_conflict_error(dataset_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.DATASET_DELETED,
        status_code=HTTPStatus.CONFLICT,
        message="This dataset has been administratively deleted; new writes are not accepted.",
        details={"dataset_id": str(dataset_id)},
    )


async def raise_if_dataset_administratively_blocked(
    uow: UnitOfWorkForBarrier, dataset_id: UUID | None
) -> None:
    """ADR-0010 D12/D16/D28: reject a new Remember/Cognify/Improve/Forget (or
    their manual retry) submission targeting a dataset under an
    administrative delete intent, an administratively-owned ``DELETING``, or
    ``DELETED``. A ``dataset_id`` of ``None`` (a true global run, e.g. Forget
    Everything) is never blocked here -- ADR-0010 D28 handles Forget
    Everything's own target-selection exclusion separately, since a global
    run has no single dataset to check against this barrier.

    An ordinary Forget-owned ``DELETING`` dataset (no administrative
    ``DATASET_DELETE`` lineage ever crossed ``begin_delete``) is
    deliberately NOT blocked -- Forget's own resumption of its own transient
    ``DELETING`` state is unaffected (ADR-0010 D28's counterexample).
    """

    if dataset_id is None:
        return
    dataset = await uow.datasets.get_by_id(dataset_id)
    if dataset is None:
        return  # target-missing is the caller's own concern, not this barrier's
    if dataset.status == DatasetStatus.DELETED:
        raise dataset_deleted_conflict_error(dataset_id)
    nonterminal = await uow.pipeline_runs.find_nonterminal_dataset_delete_for_dataset(dataset_id)
    if nonterminal is not None:
        raise dataset_deleting_conflict_error(dataset_id)
    if dataset.status == DatasetStatus.DELETING and (
        await uow.pipeline_runs.exists_administrative_delete_ownership(dataset_id)
    ):
        raise dataset_deleting_conflict_error(dataset_id)


__all__ = [
    "UnitOfWorkForBarrier",
    "dataset_deleted_conflict_error",
    "dataset_deleting_conflict_error",
    "raise_if_dataset_administratively_blocked",
]
