from __future__ import annotations

from enum import StrEnum

from sofias_memory.domain import (
    DatasetStatus,
    GraphOutboxOperation,
    GraphOutboxStatus,
    MemoryEntryType,
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
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


def test_pipeline_type_values_are_exact_from_adr_0007_and_adr_0010() -> None:
    assert [pipeline_type.value for pipeline_type in PipelineType] == [
        "remember",
        "cognify",
        "improve",
        "forget",
        "dataset_delete",
    ]


def test_pipeline_run_status_values_are_exact_from_adr_0007_without_stale() -> None:
    assert [status.value for status in PipelineRunStatus] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelling",
        "cancelled",
    ]
    assert "stale" not in {status.value for status in PipelineRunStatus}


def test_pipeline_step_status_values_are_exact_from_adr_0007_without_stale() -> None:
    assert [status.value for status in PipelineStepStatus] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelling",
        "cancelled",
    ]
    assert "stale" not in {status.value for status in PipelineStepStatus}


def test_graph_outbox_operation_and_status_values_are_exact() -> None:
    assert [operation.value for operation in GraphOutboxOperation] == ["upsert", "delete"]
    assert [status.value for status in GraphOutboxStatus] == [
        "pending",
        "processing",
        "done",
        "failed",
    ]


def test_sm211_enums_are_str_enums_and_serialize_lowercase_values() -> None:
    assert issubclass(PipelineType, StrEnum)
    assert issubclass(PipelineRunStatus, StrEnum)
    assert issubclass(PipelineStepStatus, StrEnum)
    assert issubclass(GraphOutboxOperation, StrEnum)
    assert issubclass(GraphOutboxStatus, StrEnum)
    assert str(PipelineType.REMEMBER) == "remember"
    assert f"{PipelineRunStatus.CANCELLING}" == "cancelling"
    assert str(GraphOutboxOperation.UPSERT) == "upsert"


def test_sm211_enums_have_no_aliases() -> None:
    assert len(PipelineType) == len({pipeline_type.value for pipeline_type in PipelineType})
    assert len(PipelineRunStatus) == len({status.value for status in PipelineRunStatus})
    assert len(PipelineStepStatus) == len({status.value for status in PipelineStepStatus})
    assert len(GraphOutboxOperation) == len({operation.value for operation in GraphOutboxOperation})
    assert len(GraphOutboxStatus) == len({status.value for status in GraphOutboxStatus})
