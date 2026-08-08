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
