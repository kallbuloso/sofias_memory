"""Public schemas for Contextual Recall (SM-CTX-006, Contract v1 Contextual
Project Memory SS 37-53, handoff 14).

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
clamped server-side to hard maximums (Contract SS 53); the Contract's limit
names are kept even for sections SM-CTX-006 never populates.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as pydantic_field_validator

from sofias_memory.domain import DEFAULT_CONTEXT_KEY, normalize_cognitive_memory_recall_query
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


class ContextRecallIntent(StrEnum):
    """Contract v1 SS 40. SM-CTX-006 implements ``resume`` and ``context``;
    ``explain``/``history`` are rejected explicitly until SM-CTX-007."""

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
            "historical_checkpoints cap; server maximum "
            f"{CONTEXT_RECALL_MAX_HISTORICAL_CHECKPOINTS}. That section is not populated yet."
        ),
    )
    knowledge: int = Field(
        default=CONTEXT_RECALL_DEFAULT_KNOWLEDGE,
        ge=1,
        description=(
            f"knowledge_references cap; server maximum {CONTEXT_RECALL_MAX_KNOWLEDGE}. "
            "That section is not populated yet."
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
            "resume | context. explain and history are part of Contract v1 but are rejected "
            "with 422 INVALID_REQUEST until they are implemented."
        )
    )
    query: str = Field(
        description=(
            "Required, 1..8192 Unicode characters after normalization. Used for semantic "
            "selection of Cognitive Memory and Episodes; never answered by an LLM."
        )
    )
    datasets: list[str] = Field(
        default_factory=list,
        description=(
            "Reserved for Knowledge Memory integration. Absent or empty is accepted; a "
            "non-empty list is rejected with 422 INVALID_REQUEST (never ignored)."
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


class ContextRecallCheckpoint(ContextCheckpointResult):
    """The current ContextCheckpoint of the Context Stream. Statements whose
    explicit support is currently insufficient are omitted from this usable
    context (the persisted checkpoint itself is never changed)."""

    result_class: Literal["current_checkpoint"] = "current_checkpoint"


class ContextRecallHistoricalCheckpoint(ContextCheckpointResult):
    """Reserved for SM-CTX-007 ``history``; never populated by SM-CTX-006."""

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
    """An ACTIVE Episode: historical experience, never current truth."""

    model_config = ConfigDict(extra="forbid")

    result_class: Literal["historical_episode"] = "historical_episode"
    episode: EpisodeResult
    relevance: float | None = Field(
        description=(
            "Cosine similarity in [-1, 1] when selected semantically; null when selected by "
            "the deterministic recency fallback (never fabricated)."
        )
    )


class ContextRecallEvidence(EvidenceResult):
    """One canonical Evidence identity referenced by a returned resource, with
    its current availability. Never source content."""

    result_class: Literal["evidence"] = "evidence"


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
        max_length=0,
        description="Reserved for the history intent (SM-CTX-007); always empty.",
    )
    knowledge_references: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=0,
        description="Reserved for Knowledge Memory integration (SM-CTX-007); always empty.",
    )
    evidence: list[ContextRecallEvidence] = Field(
        description="Aggregated Evidence when include_evidence=true; otherwise empty."
    )
    warnings: list[ContextRecallWarning]
    degraded_sources: list[ContextRecallSource]
