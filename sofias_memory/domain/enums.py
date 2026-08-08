"""Stable domain enums shared by persistence and application code."""

from __future__ import annotations

from enum import StrEnum


class DatasetStatus(StrEnum):
    """Lifecycle status for datasets."""

    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class SourceKind(StrEnum):
    """Accepted source input kinds."""

    TEXT = "text"
    FILE = "file"
    URL = "url"


class SourceStatus(StrEnum):
    """Lifecycle status for sources."""

    PENDING = "pending"
    PROCESSING = "processing"
    ACTIVE = "active"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class SummaryTargetType(StrEnum):
    """Supported summary target categories."""

    DOCUMENT = "document"
    ENTITY = "entity"
    DATASET = "dataset"
    CLUSTER = "cluster"


class MemoryEntryType(StrEnum):
    """Persisted lightweight memory entry categories."""

    TEXT = "text"
    QA = "qa"
    FEEDBACK = "feedback"
    NOTE = "note"


class PipelineType(StrEnum):
    """Write pipeline kinds that create durable runs."""

    REMEMBER = "remember"
    COGNIFY = "cognify"
    IMPROVE = "improve"
    FORGET = "forget"


class PipelineRunStatus(StrEnum):
    """Lifecycle status for persisted pipeline runs."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class PipelineStepStatus(StrEnum):
    """Lifecycle status for persisted pipeline steps."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class GraphOutboxOperation(StrEnum):
    """Graph projection operation type."""

    UPSERT = "upsert"
    DELETE = "delete"


class GraphOutboxStatus(StrEnum):
    """Graph outbox processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
