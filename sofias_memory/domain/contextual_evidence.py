"""Contextual Evidence primitives (SM-CTX-004, ADR-0019, Contract v1
Contextual Project Memory SS 11-13, handoff 12).

``Provenance != Evidence``: only an explicitly declared EvidenceRef creates a
support dependency. Nothing here infers Evidence from provenance, Session
association, scope, text, time or similarity.

Pure functions only (no FastAPI/SQLAlchemy/Neo4j): availability mapping per
source type from the authoritative lifecycle fields, deterministic support
evaluation, canonical (unordered, duplicate-free) support sets and the explicit
project boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sofias_memory.domain.enums import (
    CognitiveMemoryLifecycle,
    DatasetStatus,
    EpisodeLifecycle,
    EvidenceAvailability,
    EvidenceSourceType,
    SourceStatus,
    SupportMode,
)

MAX_EVIDENCE_REFS_PER_SUPPORT = 50
"""Operational server-side bound per declared support set (handoff 12 SS 49):
generous for legitimate claims, prevents abusive payloads and huge graphs."""

GLOBAL_MEMORY_SCOPE = "global"


@dataclass(frozen=True, slots=True, order=True)
class EvidenceKey:
    """Canonical Evidence identity ``(source_type, source_id)`` (handoff 12 SS 12).
    Ordering is the deterministic canonical order (source_type, source_id)."""

    source_type: EvidenceSourceType
    source_id: UUID


class DuplicateEvidenceReferenceError(ValueError):
    """The same ``(source_type, source_id)`` was declared twice in one support set."""

    def __init__(self, key: EvidenceKey) -> None:
        self.key = key
        super().__init__(f"duplicate Evidence reference {key.source_type.value}:{key.source_id}")


def canonical_evidence_keys(keys: Iterable[EvidenceKey]) -> list[EvidenceKey]:
    """Evidence is a set: sorted by (source_type, source_id); duplicates are a
    caller error and rejected (handoff 12 SS 25/26)."""

    seen: set[EvidenceKey] = set()
    for key in keys:
        if key in seen:
            raise DuplicateEvidenceReferenceError(key)
        seen.add(key)
    return sorted(seen, key=lambda k: (k.source_type.value, str(k.source_id)))


def canonical_support_payload(mode: SupportMode, keys: Sequence[EvidenceKey]) -> dict[str, object]:
    """Idempotency representation of a declared support: mode + canonical set.
    Order of the request array never changes it (handoff 12 SS 71/73)."""

    return {
        "mode": mode.value,
        "refs": [
            {"source_type": key.source_type.value, "source_id": str(key.source_id)}
            for key in canonical_evidence_keys(keys)
        ],
    }


# --- availability mapping (handoff 12 SS 38-45) ----------------------------------


def session_entry_availability(*, exists: bool) -> EvidenceAvailability:
    """Row exists -> AVAILABLE (a Session archive is not Forget); missing ->
    UNAVAILABLE (SessionEntry has no Forget tombstone in the baseline)."""

    return EvidenceAvailability.AVAILABLE if exists else EvidenceAvailability.UNAVAILABLE


def memory_item_availability(lifecycle: CognitiveMemoryLifecycle | None) -> EvidenceAvailability:
    """ACTIVE/SUPERSEDED -> AVAILABLE (superseded is valid historical Evidence);
    FORGOTTEN -> FORGOTTEN; missing -> UNAVAILABLE."""

    if lifecycle is None:
        return EvidenceAvailability.UNAVAILABLE
    if lifecycle is CognitiveMemoryLifecycle.FORGOTTEN:
        return EvidenceAvailability.FORGOTTEN
    if lifecycle in (CognitiveMemoryLifecycle.ACTIVE, CognitiveMemoryLifecycle.SUPERSEDED):
        return EvidenceAvailability.AVAILABLE
    return EvidenceAvailability.UNAVAILABLE


def episode_availability(lifecycle: EpisodeLifecycle | None) -> EvidenceAvailability:
    """ACTIVE -> AVAILABLE; INVALIDATED -> UNAVAILABLE; FORGOTTEN -> FORGOTTEN;
    missing -> UNAVAILABLE."""

    if lifecycle is EpisodeLifecycle.ACTIVE:
        return EvidenceAvailability.AVAILABLE
    if lifecycle is EpisodeLifecycle.FORGOTTEN:
        return EvidenceAvailability.FORGOTTEN
    return EvidenceAvailability.UNAVAILABLE


def _knowledge_forgotten(source_status: SourceStatus, dataset_status: DatasetStatus) -> bool:
    """Authoritative destructive deletion of the Knowledge ancestor: a Forget or
    a Dataset delete ends with ``Source.status = deleted``."""

    return source_status is SourceStatus.DELETED or dataset_status is DatasetStatus.DELETED


def source_availability(
    *, source_status: SourceStatus | None, dataset_status: DatasetStatus | None
) -> EvidenceAvailability:
    """ACTIVE Source in an ACTIVE Dataset -> AVAILABLE; deleted -> FORGOTTEN;
    pending/processing/failed/deleting or missing -> UNAVAILABLE."""

    if source_status is None or dataset_status is None:
        return EvidenceAvailability.UNAVAILABLE
    if _knowledge_forgotten(source_status, dataset_status):
        return EvidenceAvailability.FORGOTTEN
    if source_status is SourceStatus.ACTIVE and dataset_status is DatasetStatus.ACTIVE:
        return EvidenceAvailability.AVAILABLE
    return EvidenceAvailability.UNAVAILABLE


def document_availability(
    *,
    document_is_active: bool | None,
    document_generation: int | None,
    source_status: SourceStatus | None,
    dataset_status: DatasetStatus | None,
    dataset_active_generation: int | None,
) -> EvidenceAvailability:
    """Mere physical existence is not Evidence (handoff 12 SS 43): AVAILABLE
    only for an active Document of the Dataset's authoritative generation whose
    Source and Dataset are ACTIVE (the Knowledge Recall eligibility rule)."""

    if (
        document_is_active is None
        or document_generation is None
        or source_status is None
        or dataset_status is None
        or dataset_active_generation is None
    ):
        return EvidenceAvailability.UNAVAILABLE
    if _knowledge_forgotten(source_status, dataset_status):
        return EvidenceAvailability.FORGOTTEN
    if (
        document_is_active
        and document_generation == dataset_active_generation
        and source_status is SourceStatus.ACTIVE
        and dataset_status is DatasetStatus.ACTIVE
    ):
        return EvidenceAvailability.AVAILABLE
    return EvidenceAvailability.UNAVAILABLE


def chunk_availability(
    *,
    chunk_is_active: bool | None,
    chunk_generation: int | None,
    document_is_active: bool | None,
    document_generation: int | None,
    source_status: SourceStatus | None,
    dataset_status: DatasetStatus | None,
    dataset_active_generation: int | None,
) -> EvidenceAvailability:
    """Same rule as Document, plus the Chunk's own ``is_active``/generation."""

    if chunk_is_active is None or chunk_generation is None:
        return EvidenceAvailability.UNAVAILABLE
    parent = document_availability(
        document_is_active=document_is_active,
        document_generation=document_generation,
        source_status=source_status,
        dataset_status=dataset_status,
        dataset_active_generation=dataset_active_generation,
    )
    if parent is not EvidenceAvailability.AVAILABLE:
        return parent
    if chunk_is_active and chunk_generation == dataset_active_generation:
        return EvidenceAvailability.AVAILABLE
    return EvidenceAvailability.UNAVAILABLE


# --- support evaluation ---------------------------------------------------------


def evaluate_support(mode: SupportMode, availabilities: Sequence[EvidenceAvailability]) -> bool:
    """Deterministic: ``all_required`` needs every ref AVAILABLE; ``any_sufficient``
    needs at least one (Contract v1 SS 12). No LLM, embedding, confidence or
    text heuristic. An empty set is never a valid support declaration."""

    if not availabilities:
        raise ValueError("a support declaration must contain at least one Evidence reference")
    if mode is SupportMode.ALL_REQUIRED:
        return all(a is EvidenceAvailability.AVAILABLE for a in availabilities)
    return any(a is EvidenceAvailability.AVAILABLE for a in availabilities)


# --- project boundary (Contract v1 SS 11.5) ----------------------------------------


def evidence_project_matches(
    *, owner_scope: str, source_type: EvidenceSourceType, source_scope: str | None
) -> bool:
    """Episode Evidence must belong to the owner's project; a project-scoped
    MemoryItem must too, while an explicitly referenced ``global`` MemoryItem is
    allowed. SessionEntry/Source/Document/Chunk carry no project ownership.
    A scrubbed (NULL) tombstone scope is not a mismatch -- its availability
    already rejects it."""

    if source_scope is None:
        return True
    if source_type is EvidenceSourceType.EPISODE:
        return source_scope == owner_scope
    if source_type is EvidenceSourceType.MEMORY_ITEM:
        return source_scope in (GLOBAL_MEMORY_SCOPE, owner_scope)
    return True
