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
    DATASET_DELETE = "dataset_delete"


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


class SessionStatus(StrEnum):
    """Lifecycle status for first-class durable Sessions."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillStatus(StrEnum):
    """Lifecycle status for first-class durable procedural Skills."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentStatus(StrEnum):
    """Lifecycle status for first-class durable Agent Profiles."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class CognitiveMemoryType(StrEnum):
    """Native Cognitive Memory type (ADR-0016, Feature Contract v0.7.0 SS 4).

    Contract v1 supports exactly ``PROFILE``/``SEMANTIC``. ``EPISODIC`` and
    ``PROCEDURAL`` are explicitly deferred and must never be added here
    without a new accepted ADR -- Skills (ADR-0013) remain the separate
    procedural domain.
    """

    PROFILE = "profile"
    SEMANTIC = "semantic"


class CognitiveMemoryLifecycle(StrEnum):
    """Native Cognitive Memory lifecycle state machine (ADR-0016 SS 10).

    Allowed transitions: ``active -> superseded``, ``active -> forgotten``,
    ``superseded -> forgotten``, ``forgotten -> forgotten`` (idempotent
    no-op). There is no restore/unforget.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class CognitiveMemoryOriginKind(StrEnum):
    """Native Cognitive Memory provenance origin kind (ADR-0016 SS 7,
    Feature Contract v0.7.0 SS 7.1)."""

    USER_ASSERTED = "user_asserted"
    TOOL_OBSERVED = "tool_observed"
    IMPORTED = "imported"
    INFERRED = "inferred"
    ASSISTANT_GENERATED = "assistant_generated"


class CognitiveMemoryOperation(StrEnum):
    """``cognitive_memory_idempotency`` ledger operation identity (ADR-0016
    SS 14). Independent of ``PipelineType`` -- Cognitive Memory mutations
    are never routed through PipelineRun."""

    CREATE = "create"
    SUPERSEDE = "supersede"
    FORGET = "forget"
