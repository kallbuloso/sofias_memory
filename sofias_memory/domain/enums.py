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
    """Native Cognitive Memory type (Feature Contract v0.7.0 SS 4).

    Contract v1 supports exactly ``PROFILE``/``SEMANTIC``. ``EPISODIC`` and
    ``PROCEDURAL`` are explicitly deferred and must never be added here
    without a new accepted architecture decision -- Skills remain the
    separate procedural domain.
    """

    PROFILE = "profile"
    SEMANTIC = "semantic"


class CognitiveMemoryLifecycle(StrEnum):
    """Native Cognitive Memory lifecycle state machine.

    Allowed transitions: ``active -> superseded``, ``active -> forgotten``,
    ``superseded -> forgotten``, ``forgotten -> forgotten`` (idempotent
    no-op). There is no restore/unforget.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class CognitiveMemoryOriginKind(StrEnum):
    """Native Cognitive Memory provenance origin kind (Feature Contract
    v0.7.0 SS 7.1)."""

    USER_ASSERTED = "user_asserted"
    TOOL_OBSERVED = "tool_observed"
    IMPORTED = "imported"
    INFERRED = "inferred"
    ASSISTANT_GENERATED = "assistant_generated"


class EpisodeKind(StrEnum):
    """Contextual Project Memory Episode kind (Contract v1 SS 15).

    Exactly the nine approved v1 kinds -- never extend without an explicit
    Contract evolution."""

    DECISION = "decision"
    INVESTIGATION = "investigation"
    EXPERIMENT = "experiment"
    INCIDENT = "incident"
    DISCOVERY = "discovery"
    CHANGE = "change"
    OUTCOME = "outcome"
    LEARNING = "learning"
    MILESTONE = "milestone"


class EpisodeLifecycle(StrEnum):
    """Contextual Project Memory Episode lifecycle (Contract v1 SS 16).

    Allowed transitions: ``active -> invalidated``, ``active -> forgotten``,
    ``invalidated -> forgotten``. ``forgotten`` is terminal. There is no
    ``superseded``, restore, or unforget for Episodes."""

    ACTIVE = "active"
    INVALIDATED = "invalidated"
    FORGOTTEN = "forgotten"


class ContextualMemoryOperation(StrEnum):
    """``contextual_memory_idempotency`` ledger operation identity.

    Resource-generic on purpose: the ledger is shared by every Contextual
    Project Memory mutation. The ``checkpoint_*`` values exist only as schema
    readiness for later Slices (SM-CTX-002/003) so the PostgreSQL enum does
    not churn -- SM-CTX-001 executes only the ``episode_*`` operations.
    Independent of ``PipelineType``: contextual mutations never create a
    PipelineRun."""

    EPISODE_CREATE = "episode_create"
    EPISODE_INVALIDATE = "episode_invalidate"
    EPISODE_FORGET = "episode_forget"
    CHECKPOINT_CREATE = "checkpoint_create"
    CHECKPOINT_SUPERSEDE = "checkpoint_supersede"
    CHECKPOINT_INVALIDATE = "checkpoint_invalidate"
    CHECKPOINT_FORGET = "checkpoint_forget"


class ContextCheckpointLifecycle(StrEnum):
    """Contextual Project Memory ContextCheckpoint lifecycle (Contract v1 SS 26).

    ``active`` is the single current checkpoint of a Context Stream;
    ``superseded`` is a revision replaced through Supersede; ``invalidated``
    is no longer current truth (explicitly, or after it lost its required
    ``current_state`` support); ``forgotten`` is a terminal scrubbed tombstone."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    FORGOTTEN = "forgotten"


class ContextStatementType(StrEnum):
    """Semantic unit type of a ContextCheckpoint (handoff 10 SS 17).

    ``objective``/``current_state``/``next_action`` are scalar (at most one
    per checkpoint); the others are ordered collections."""

    OBJECTIVE = "objective"
    CURRENT_STATE = "current_state"
    APPROVED_DECISION = "approved_decision"
    PENDING_WORK = "pending_work"
    OPEN_ITEM = "open_item"
    BLOCKER = "blocker"
    NEXT_ACTION = "next_action"


class EvidenceSourceType(StrEnum):
    """Contextual Evidence source types (Contract v1 SS 11.1).

    Exactly these six in v1. ``source`` means the Knowledge ``Source``
    resource."""

    SESSION_ENTRY = "session_entry"
    MEMORY_ITEM = "memory_item"
    EPISODE = "episode"
    SOURCE = "source"
    DOCUMENT = "document"
    CHUNK = "chunk"


class EvidenceAvailability(StrEnum):
    """Current availability of one Evidence source (Contract v1 SS 11.2). Always
    computed from the authoritative source state, never cached."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FORGOTTEN = "forgotten"


class SupportMode(StrEnum):
    """Declared support policy (Contract v1 SS 12). Evaluated deterministically."""

    ALL_REQUIRED = "all_required"
    ANY_SUFFICIENT = "any_sufficient"


class CognitiveMemoryOperation(StrEnum):
    """``cognitive_memory_idempotency`` ledger operation identity.
    Independent of ``PipelineType`` -- Cognitive Memory mutations are never
    routed through PipelineRun."""

    CREATE = "create"
    SUPERSEDE = "supersede"
    FORGET = "forget"
