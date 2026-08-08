from __future__ import annotations

from enum import StrEnum

from sofias_memory.domain import (
    DatasetStatus,
    MemoryEntryType,
    SourceKind,
    SourceStatus,
    SummaryTargetType,
)


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


def test_summary_target_type_values_are_exact() -> None:
    assert [target_type.value for target_type in SummaryTargetType] == [
        "document",
        "entity",
        "dataset",
        "cluster",
    ]


def test_memory_entry_type_values_are_exact() -> None:
    assert [entry_type.value for entry_type in MemoryEntryType] == [
        "text",
        "qa",
        "feedback",
        "note",
    ]


def test_sm210_enums_are_str_enums_and_serialize_lowercase_values() -> None:
    assert issubclass(SummaryTargetType, StrEnum)
    assert issubclass(MemoryEntryType, StrEnum)
    assert str(SummaryTargetType.DOCUMENT) == "document"
    assert f"{MemoryEntryType.QA}" == "qa"


def test_sm210_enums_have_no_aliases() -> None:
    assert len(SummaryTargetType) == len({target_type.value for target_type in SummaryTargetType})
    assert len(MemoryEntryType) == len({entry_type.value for entry_type in MemoryEntryType})
