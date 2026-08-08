from __future__ import annotations

from sofias_memory.domain import DatasetStatus, SourceKind, SourceStatus


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


def test_source_kind_values_are_exact() -> None:
    assert [kind.value for kind in SourceKind] == ["text", "file", "url"]


def test_source_status_values_are_exact_from_adr_0007() -> None:
    assert [status.value for status in SourceStatus] == [
        "pending",
        "processing",
        "active",
        "failed",
        "deleting",
        "deleted",
    ]


def test_source_enums_serialize_as_string_values() -> None:
    assert str(SourceKind.FILE) == "file"
    assert f"{SourceStatus.PROCESSING}" == "processing"


def test_source_enums_have_no_aliases() -> None:
    assert len(SourceKind) == len({kind.value for kind in SourceKind})
    assert len(SourceStatus) == len({status.value for status in SourceStatus})
