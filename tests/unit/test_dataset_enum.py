from __future__ import annotations

from sofias_memory.domain import DatasetStatus


def test_dataset_status_values_are_exact() -> None:
    assert [status.value for status in DatasetStatus] == ["active", "deleting", "deleted"]


def test_dataset_status_serializes_as_string_value() -> None:
    assert str(DatasetStatus.ACTIVE) == "active"
    assert f"{DatasetStatus.DELETING}" == "deleting"


def test_dataset_status_has_no_aliases() -> None:
    assert len(DatasetStatus) == len({status.value for status in DatasetStatus})


def test_dataset_status_contains_only_adr_values() -> None:
    assert set(DatasetStatus) == {
        DatasetStatus.ACTIVE,
        DatasetStatus.DELETING,
        DatasetStatus.DELETED,
    }
