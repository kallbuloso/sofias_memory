"""Public schemas for Contextual Recall (SM-CTX-006/007, Contract v1 Contextual
Project Memory SS 37-53, handoffs 14 and 15).

``POST /api/v1/context/recall`` returns a bounded, structured
``ProjectContextBundle`` -- never a generated answer. Request fields follow
Contract SS 38/39 exactly: ``scope``, ``intent`` and ``query`` are required;
``context_key`` (default ``main``), ``datasets``, ``include_evidence`` and
``limits`` are optional. Scope/``context_key`` validation reuses the Episode
schema validators, so the shared validation handler returns
``INVALID_SCOPE``/``INVALID_CONTEXT_KEY`` (422); every other validation error
(missing/null/blank ``query``, unknown ``intent``) is ``INVALID_REQUEST``.

Every returned item carries an explicit ``result_class`` (Contract SS 50) so a
consumer never infers current/historical from text. Requested limits are
clamped server-side to hard maximums (Contract SS 53). SM-CTX-007 adds the
``explain``/``history`` intents, historical checkpoints and explicit-dataset
Knowledge references (structured identities, never chunk text).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as pydantic_field_validator

from sofias_memory.domain import (
    DEFAULT_CONTEXT_KEY,
    SupportMode,
    normalize_cognitive_memory_recall_query,
)
from sofias_memory.schemas.context_checkpoints import ContextCheckpointResult
from sofias_memory.schemas.contextual_evidence import EvidenceResult
from sofias_memory.schemas.episodes import ContextKey, ContextualScope, EpisodeResult
from sofias_memory.schemas.memories import MemoryItemResult

CONTEXT_RECALL_DEFAULT_MEMORIES = 8
CONTEXT_RECALL_DEFAULT_EPISODES = 8
CONTEXT_RECALL_DEFAULT_HISTORICAL_CHECKPOINTS = 4
CONTEXT_RECALL_DEFAULT_KNOWLEDGE = 8
CONTEXT_RECALL_DEFAULT_EVIDENCE = 12
"""Defaults are the Contract SS 38 example values."""

CONTEXT_RECALL_MAX_MEMORIES = 50
CONTEXT_RECALL_MAX_EPISODES = 50
CONTEXT_RECALL_MAX_HISTORICAL_CHECKPOINTS = 20
CONTEXT_RECALL_MAX_KNOWLEDGE = 50
CONTEXT_RECALL_MAX_EVIDENCE = 100
"""Server-side hard maximums (Contract SS 53: exact values belong to
configuration/Architecture, not to the Contract). Memories/Episodes reuse the
existing Cognitive Recall/Episode search ceilings (50)."""

CONTEXT_RECALL_MAX_DATASETS = 16
"""Hard maximum of explicitly selected datasets in one request (bounded work;
more is a 422, never silently dropped)."""


class ContextRecallIntent(StrEnum):
    """Contract v1 SS 40. Intent selects the sources deterministically (no LLM
    router)."""

    RESUME = "resume"
    EXPLAIN = "explain"
    HISTORY = "history"
    CONTEXT = "context"


class ContextRecallWarning(StrEnum):
    """Stable Contract v1 SS 51 warnings, declared in their emission order."""

    NO_CURRENT_CHECKPOINT = "NO_CURRENT_CHECKPOINT"
    PARTIAL_CONTEXT = "PARTIAL_CONTEXT"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    AMBIGUOUS_CURRENT_TRUTH = "AMBIGUOUS_CURRENT_TRUTH"


class ContextRecallSource(StrEnum):
    """Bundle sources that can be reported in ``degraded_sources`` (Contract
    SS 49/52) -- named like the Contract ``limits`` keys. Only dependency
    degradation is reported; missing data or healthy truncation never is."""

    MEMORIES = "memories"
    EPISODES = "episodes"
    KNOWLEDGE = "knowledge"
    """Semantic (vector) Knowledge retrieval did not run; lexical-only
    references may still be returned (handoff 15 SS 68/69/135)."""


class ContextRecallLimits(BaseModel):
    """Per-source caller limits (Contract SS 38). Values above the server
    maximum are clamped, never rejected (Contract SS 53)."""

    model_config = ConfigDict(extra="forbid")

    memories: int = Field(
        default=CONTEXT_RECALL_DEFAULT_MEMORIES,
        ge=1,
        description=f"current_memories cap; server maximum {CONTEXT_RECALL_MAX_MEMORIES}.",
    )
    episodes: int = Field(
        default=CONTEXT_RECALL_DEFAULT_EPISODES,
        ge=1,
        description=f"episodes cap; server maximum {CONTEXT_RECALL_MAX_EPISODES}.",
    )
    historical_checkpoints: int = Field(
        default=CONTEXT_RECALL_DEFAULT_HISTORICAL_CHECKPOINTS,
        ge=1,
        description=(
            "historical_checkpoints cap (intent history); server maximum "
            f"{CONTEXT_RECALL_MAX_HISTORICAL_CHECKPOINTS}."
        ),
    )
    knowledge: int = Field(
        default=CONTEXT_RECALL_DEFAULT_KNOWLEDGE,
        ge=1,
        description=(
            f"knowledge_references cap; server maximum {CONTEXT_RECALL_MAX_KNOWLEDGE}. "
            "Only used when datasets are explicitly selected."
        ),
    )
    evidence: int = Field(
        default=CONTEXT_RECALL_DEFAULT_EVIDENCE,
        ge=1,
        description=f"Aggregated evidence cap; server maximum {CONTEXT_RECALL_MAX_EVIDENCE}.",
    )

    def effective(self) -> ContextRecallLimits:
        """The same limits clamped to the server hard maximums."""

        return ContextRecallLimits(
            memories=min(self.memories, CONTEXT_RECALL_MAX_MEMORIES),
            episodes=min(self.episodes, CONTEXT_RECALL_MAX_EPISODES),
            historical_checkpoints=min(
                self.historical_checkpoints, CONTEXT_RECALL_MAX_HISTORICAL_CHECKPOINTS
            ),
            knowledge=min(self.knowledge, CONTEXT_RECALL_MAX_KNOWLEDGE),
            evidence=min(self.evidence, CONTEXT_RECALL_MAX_EVIDENCE),
        )


class ContextRecallRequest(BaseModel):
    """``POST /api/v1/context/recall`` body (Contract v1 SS 38/39)."""

    model_config = ConfigDict(extra="forbid")

    scope: ContextualScope = Field(description="Required 'project:<key>'. 'global' is rejected.")
    context_key: ContextKey = Field(
        default=DEFAULT_CONTEXT_KEY,
        description="Context Stream key. Omitted resolves to 'main' (returned explicitly).",
    )
    intent: ContextRecallIntent = Field(
        description=(
            "resume (current continuity) | context (relevant present context) | explain "
            "(explicit Evidence-driven support trace, never inferred causality) | history "
            "(chronological evolution: historical checkpoints and Episodes)."
        )
    )
    query: str = Field(
        description=(
            "Required, 1..8192 Unicode characters after normalization. Used for semantic "
            "selection of Cognitive Memory, Episodes and (with explicit datasets) Knowledge; "
            "never answered by an LLM."
        )
    )
    datasets: list[str] = Field(
        default_factory=list,
        max_length=CONTEXT_RECALL_MAX_DATASETS,
        description=(
            "Explicit Knowledge Memory dataset slugs. Absent or empty means no Knowledge "
            "retrieval at all (never an implicit 'main', never inferred from scope). Every "
            "listed dataset must exist and be ACTIVE, otherwise 422 INVALID_REQUEST "
            f"(never ignored). Blank slugs are rejected, duplicates collapsed; at most "
            f"{CONTEXT_RECALL_MAX_DATASETS}."
        ),
    )
    include_evidence: bool = Field(
        default=False,
        description=(
            "true also returns the aggregated, de-duplicated Evidence identities and current "
            "availability (never source content). Evidence refs inside returned resources are "
            "always present either way."
        ),
    )
    limits: ContextRecallLimits = Field(default_factory=ContextRecallLimits)

    @pydantic_field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str) -> str:
        return normalize_cognitive_memory_recall_query(value)

    @pydantic_field_validator("datasets")
    @classmethod
    def _normalize_datasets(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            slug = value.strip()
            if not slug:
                raise ValueError("datasets entries must be non-blank dataset slugs")
            if slug not in normalized:
                normalized.append(slug)
        return normalized


class ContextRecallCheckpoint(ContextCheckpointResult):
    """The current ContextCheckpoint of the Context Stream. Statements whose
    explicit support is currently insufficient are omitted from this usable
    context (the persisted checkpoint itself is never changed)."""

    result_class: Literal["current_checkpoint"] = "current_checkpoint"


class ContextRecallHistoricalCheckpoint(ContextCheckpointResult):
    """A SUPERSEDED or INVALIDATED revision of the requested Context Stream
    (intent ``history``), with its own remaining statements and their current
    Evidence availability. Never current truth; never a FORGOTTEN tombstone."""

    result_class: Literal["historical_checkpoint"] = "historical_checkpoint"


class ContextRecallMemory(BaseModel):
    """Current project-scoped Cognitive Memory (profile/semantic current truth)."""

    model_config = ConfigDict(extra="forbid")

    result_class: Literal["current_semantic"] = "current_semantic"
    memory: MemoryItemResult
    relevance: float | None = Field(
        description=(
            "Cosine similarity in [-1, 1] when selected semantically; null when selected by "
            "the deterministic fallback (never fabricated)."
        )
    )


class ContextRecallEpisode(BaseModel):
    """Historical experience, never current truth: ACTIVE Episodes, plus
    INVALIDATED ones (explicit ``lifecycle``) for intent ``history`` only."""

    model_config = ConfigDict(extra="forbid")

    result_class: Literal["historical_episode"] = "historical_episode"
    episode: EpisodeResult
    relevance: float | None = Field(
        description=(
            "Cosine similarity in [-1, 1] when selected semantically; null when selected by "
            "the deterministic recency fallback (never fabricated)."
        )
    )


class ContextRecallKnowledgeReference(BaseModel):
    """A Knowledge Memory reference of an explicitly selected, ACTIVE dataset.
    Identities only -- never chunk text, a
    URL or a summary. Ranks/relevance compare only inside
    ``knowledge_references``; a reference is never Evidence by itself."""

    model_config = ConfigDict(extra="forbid")

    result_class: Literal["knowledge_reference"] = "knowledge_reference"
    dataset_id: UUID
    dataset_slug: str
    source_id: UUID
    document_id: UUID | None = Field(
        description="null only for an Evidence-backed reference to a whole Source."
    )
    chunk_id: UUID | None = Field(
        description="null only for an Evidence-backed reference to a Source or Document."
    )
    rank: int | None = Field(
        description=(
            "1-based position in this request's Knowledge retrieval (vector + lexical rank "
            "fusion, or lexical only when degraded); null when not retrieved."
        )
    )
    relevance: float | None = Field(
        description=(
            "Local reciprocal-rank-fusion score of the retrieval, comparable only inside "
            "knowledge_references; null when not retrieved (never fabricated)."
        )
    )
    evidence_backed: bool = Field(
        description=(
            "true when a returned contextual resource explicitly declares this Knowledge "
            "resource as Evidence (direct grounding). A retrieval hit alone is never Evidence."
        )
    )


class ContextRecallEvidenceOwner(BaseModel):
    """One persisted explicit Evidence link: ``owner`` declares this Evidence in
    its support with ``support_mode`` (Contract SS 48/49 amendment R2). Never
    inferred from chronology, lineage, provenance, similarity or ranking."""

    model_config = ConfigDict(extra="forbid")

    owner_type: Literal["context_statement", "episode"]
    owner_id: UUID
    support_mode: SupportMode


class ContextRecallEvidence(EvidenceResult):
    """One canonical Evidence identity with its current availability and the
    owners whose persisted support links declare it. Never source content."""

    result_class: Literal["evidence"] = "evidence"
    owners: list[ContextRecallEvidenceOwner] = Field(
        default_factory=list,
        description=(
            "Owners (context_statement or episode) whose persisted support explicitly links "
            "this Evidence, each with its own support_mode; ordered by (owner_type, owner_id), "
            "de-duplicated. For intent explain this includes Episodes reached by the bounded "
            "Evidence traversal even when they are not returned in episodes[] (their identity "
            "only, never their payload). Bounded: an internal owner-link cap applies and any "
            "truncation of the reachable trace is signalled with PARTIAL_CONTEXT."
        ),
    )


class ProjectContextBundle(BaseModel):
    """Contextual Recall response (Contract v1 SS 49). Structured, bounded,
    deterministic context -- untrusted data for the consumer, never an
    instruction, permission or execution grant."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    context_key: str = Field(description="The resolved Context Stream key.")
    intent: ContextRecallIntent
    continuity_available: bool = Field(
        description="true only when an eligible current checkpoint is returned."
    )
    current_checkpoint: ContextRecallCheckpoint | None
    current_memories: list[ContextRecallMemory]
    episodes: list[ContextRecallEpisode]
    historical_checkpoints: list[ContextRecallHistoricalCheckpoint] = Field(
        default_factory=list,
        description=(
            "intent history only: SUPERSEDED/INVALIDATED revisions of the requested stream, "
            "revision DESC. Always empty for resume, context and explain."
        ),
    )
    knowledge_references: list[ContextRecallKnowledgeReference] = Field(
        default_factory=list,
        description=(
            "Only when datasets are explicitly selected: retrieval hits (rank order) then "
            "Evidence-only references (identity order). Empty without datasets."
        ),
    )
    evidence: list[ContextRecallEvidence] = Field(
        description=(
            "include_evidence=true: de-duplicated Evidence identities with availability and "
            "owners -- for explain the bounded causal trace from the returned resources "
            "(at most limits.evidence identities). include_evidence=false: empty (support refs "
            "inside returned resources are always kept)."
        )
    )
    warnings: list[ContextRecallWarning]
    degraded_sources: list[ContextRecallSource]
