"""Improve v1 pure/reusable primitives (SM-511).

This module no longer owns any run lifecycle (that is
``sofias_memory.pipelines.steps.improve``, ADR-0009 SS O, mirroring SM-510's
``services.cognify``/``pipelines.steps.cognify`` split): it holds only the
business-logic building blocks the B5 Improve pipeline steps compose --
feedback-weight math, embedding-text formatting, entity-merge planning, and
relation-merge planning. Every function here is either pure or performs only
read-only PostgreSQL access; none of them commits, and none of them is ever
called from a ``PipelineStep.persist`` (ADR-0009 SS O forbids external I/O
and independent commits there).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Protocol
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.infrastructure.postgres.models import (
    Entity,
    EntityMention,
    Relation,
    RelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.entities import (
    EntityDuplicateCandidate,
    EntityEmbeddingCandidate,
)
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.infrastructure.postgres.repositories.relations import RelationEmbeddingCandidate
from sofias_memory.ports import (
    ProjectionCommand,
    entity_mention_upsert_command,
    relation_upsert_command,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue
from sofias_memory.services.feedback import (
    ANSWER_TARGET_TYPE,
    REFERENCE_TARGET_TYPE,
    reference_chunk_ids,
)
from sofias_memory.services.graph_maintenance_service import (
    ImportanceComponents,
    has_importance_marker,
    importance_components_from_properties,
    properties_with_importance_marker,
)

FEEDBACK_WEIGHTS_STAGE = "feedback_weights"
RELATION_EMBEDDINGS_STAGE = "relation_embeddings"
ENTITY_DEDUPLICATION_STAGE = "entity_deduplication"
SUMMARIES_STAGE = "summaries"
GRAPH_RECONCILIATION_STAGE = "graph_reconciliation"
DEFAULT_IMPROVE_STAGES = (
    FEEDBACK_WEIGHTS_STAGE,
    ENTITY_DEDUPLICATION_STAGE,
    RELATION_EMBEDDINGS_STAGE,
)
SUPPORTED_IMPROVE_STAGES = frozenset(
    (
        FEEDBACK_WEIGHTS_STAGE,
        ENTITY_DEDUPLICATION_STAGE,
        RELATION_EMBEDDINGS_STAGE,
        SUMMARIES_STAGE,
        GRAPH_RECONCILIATION_STAGE,
    )
)
IMPROVE_RESULT_METRIC_KEY = "improve_result"
FEEDBACK_WEIGHT_ALPHA = 0.1
FEEDBACK_WEIGHT_DECIMALS = 4


def normalize_improve_stages(stages: Sequence[str] | None) -> list[str]:
    """Canonicalize a request's ``stages`` for both execution and hashing --
    B4 parity, preserved exactly (SM-511 MAJOR 1 correction).

    ``None`` defaults to :data:`DEFAULT_IMPROVE_STAGES`. Every valid request
    is deduplicated preserving the FIRST occurrence, and -- critically --
    request order is otherwise preserved unchanged: two requests naming the
    same stages in a different order are different durable work, hash
    differently, and execute in different (public) order. The static,
    code-defined B5 pipeline (ADR-0009 SS O) still has no per-request
    pipeline: order is realized through a fixed set of execution *slots*
    (``pipelines.steps.improve.resolve_slot_stages``), each of whose runtime
    identity is derived purely from this normalized list's position -- never
    through a dynamically constructed ``PipelineDefinition``.
    """

    requested = list(DEFAULT_IMPROVE_STAGES) if stages is None else [str(stage) for stage in stages]
    unsupported = [stage for stage in requested if stage not in SUPPORTED_IMPROVE_STAGES]
    if unsupported:
        unsupported_json: list[JSONValue] = list(unsupported)
        supported_json: list[JSONValue] = list(sorted(SUPPORTED_IMPROVE_STAGES))
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="One or more improve stages are not available.",
            details={"unsupported": unsupported_json, "supported": supported_json},
        )
    return list(dict.fromkeys(requested))


def improve_run_input(dataset: str, stages: list[str]) -> dict[str, JSONValue]:
    """The durable work identity (ADR-0009 SS S). ``wait`` is deliberately
    never part of this -- :func:`~sofias_memory.pipelines.hashing.
    canonical_work_payload_hash` has no parameter for it, by design."""

    stages_json: list[JSONValue] = list(stages)
    return {"dataset": dataset, "stages": stages_json}


def improve_stages_from_run_input(run_input: dict[str, JSONValue]) -> list[str]:
    stages = run_input.get("stages")
    if not isinstance(stages, list):
        return []
    return [str(stage) for stage in stages]


def dataset_not_found_error(dataset_slug: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Dataset does not exist.",
        details={"dataset": dataset_slug},
    )


def normalize_feedback_score(score: int) -> float:
    if score == -1:
        return 0.0
    if score == 0:
        return 0.5
    if score == 1:
        return 1.0
    raise ValueError("feedback score must be -1, 0, or 1")


def stream_update_weight(
    previous_weight: float,
    normalized_feedback: float,
    *,
    alpha: float = FEEDBACK_WEIGHT_ALPHA,
) -> float:
    if alpha <= 0 or alpha > 1:
        raise ValueError("alpha must be greater than 0 and less than or equal to 1")
    updated = previous_weight + alpha * (normalized_feedback - previous_weight)
    clamped = max(0.0, min(1.0, float(updated)))
    return round(clamped, FEEDBACK_WEIGHT_DECIMALS)


def apply_feedback_to_importance(
    *,
    properties: dict[str, object],
    current_importance_weight: float,
    normalized_score: float,
) -> tuple[float, dict[str, object]]:
    if not has_importance_marker(properties):
        return stream_update_weight(current_importance_weight, normalized_score), properties

    components = importance_components_from_properties(
        properties,
        fallback_feedback_weight=current_importance_weight,
    )
    next_feedback_weight = stream_update_weight(components.feedback_weight, normalized_score)
    next_properties = properties_with_importance_marker(
        properties,
        ImportanceComponents(
            feedback_weight=next_feedback_weight,
            centrality_weight=components.centrality_weight,
        ),
    )
    next_components = importance_components_from_properties(
        next_properties,
        fallback_feedback_weight=current_importance_weight,
    )
    return next_components.effective_weight, next_properties


def relation_embedding_text(candidate: RelationEmbeddingCandidate) -> str:
    source_name = candidate.source_name.strip()
    target_name = candidate.target_name.strip()
    predicate = candidate.predicate.strip()
    description = candidate.description.strip()
    relationship_text = predicate if not description else f"{predicate}: {description}"
    return f"{source_name}-›{relationship_text}-›{target_name}"


def entity_embedding_text(candidate: EntityEmbeddingCandidate) -> str:
    return candidate.name.strip()


def validate_embedding_response(
    embeddings: Sequence[Sequence[float]],
    *,
    expected_count: int,
    expected_dimensions: int,
    subject: str,
) -> None:
    if len(embeddings) != expected_count:
        raise DependencyUnavailableError(
            message=f"{subject} embedding provider returned an invalid response.",
            details={"reason": "count_mismatch"},
        )
    for embedding in embeddings:
        if len(embedding) != expected_dimensions:
            raise DependencyUnavailableError(
                message=f"{subject} embedding provider returned an invalid response.",
                details={"reason": "dimension_mismatch"},
            )


def feedback_target_chunk_ids(feedback: UnappliedFeedback) -> list[UUID]:
    if feedback.target_type == REFERENCE_TARGET_TYPE:
        return [feedback.target_id] if feedback.target_id is not None else []
    if feedback.target_type == ANSWER_TARGET_TYPE:
        return sorted(reference_chunk_ids(feedback.references))
    return []


def plan_entity_merges(
    entities_by_id: dict[UUID, Entity],
    candidates: Sequence[EntityDuplicateCandidate],
) -> dict[UUID, UUID]:
    adjacency: dict[UUID, set[UUID]] = {}
    for candidate in candidates:
        if (
            candidate.entity_id not in entities_by_id
            or candidate.candidate_id not in entities_by_id
        ):
            continue
        adjacency.setdefault(candidate.entity_id, set()).add(candidate.candidate_id)
        adjacency.setdefault(candidate.candidate_id, set()).add(candidate.entity_id)

    consumed: set[UUID] = set()
    entity_id_mapping: dict[UUID, UUID] = {}
    for survivor in sorted(entities_by_id.values(), key=_entity_survivor_sort_key):
        if survivor.id in consumed:
            continue
        direct_neighbors = [
            entity_id
            for entity_id in adjacency.get(survivor.id, set())
            if entity_id not in consumed and entity_id != survivor.id
        ]
        for duplicate_id in sorted(
            direct_neighbors,
            key=lambda entity_id: _entity_survivor_sort_key(entities_by_id[entity_id]),
        ):
            if duplicate_id in consumed:
                continue
            entity_id_mapping[duplicate_id] = survivor.id
            consumed.add(duplicate_id)
    return entity_id_mapping


def merged_entity_aliases(*, survivor: Entity, duplicate: Entity) -> list[str]:
    survivor_name_key = survivor.name.strip().casefold()
    aliases: list[str] = []
    seen: set[str] = set()
    for raw_alias in [*survivor.aliases, duplicate.name, *duplicate.aliases]:
        alias = raw_alias.strip()
        alias_key = alias.casefold()
        if not alias or alias_key == survivor_name_key or alias_key in seen:
            continue
        aliases.append(alias)
        seen.add(alias_key)
    return aliases


def reassign_entity_mentions(
    *,
    mentions: Sequence[EntityMention],
    entity_id_mapping: dict[UUID, UUID],
    dataset_id: UUID,
) -> list[ProjectionCommand]:
    commands: list[ProjectionCommand] = []
    for mention in sorted(mentions, key=lambda item: item.id):
        survivor_id = entity_id_mapping.get(mention.entity_id)
        if survivor_id is None:
            continue
        mention.entity_id = survivor_id
        commands.append(
            entity_mention_upsert_command(
                mention_id=mention.id,
                dataset_id=dataset_id,
                entity_id=survivor_id,
                chunk_id=mention.chunk_id,
                confidence=float(mention.confidence),
            )
        )
    return commands


@dataclass(frozen=True)
class RelationRewireApply:
    """One relation-merge group's plan (SM-511): everything ``persist`` needs
    to reapply this group's outcome to freshly-loaded ORM rows, with no
    document/quote text -- only identifiers and numbers."""

    survivor_relation_id: UUID
    changed: bool
    source_entity_id: UUID
    target_entity_id: UUID
    confidence: float
    importance_weight: float
    loser_relation_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class EvidenceCopyApply:
    survivor_relation_id: UUID
    chunk_id: UUID
    quote: str
    confidence: float


@dataclass(frozen=True)
class RelationMergeApplyPlan:
    self_loop_deactivate_ids: tuple[UUID, ...]
    applies: tuple[RelationRewireApply, ...]
    evidence_copies: tuple[EvidenceCopyApply, ...]


@dataclass(frozen=True)
class RelationMergeCounts:
    rewired: int
    deactivated: int
    evidence_copied: int


@dataclass(frozen=True)
class _RelationMergePlan:
    relation: Relation
    mapped_source_entity_id: UUID
    mapped_target_entity_id: UUID
    changed: bool
    self_loop: bool

    @property
    def identity(self) -> tuple[UUID, UUID, str, int]:
        return (
            self.mapped_source_entity_id,
            self.mapped_target_entity_id,
            self.relation.predicate,
            self.relation.generation,
        )


def plan_relation_merges(
    *,
    relations: Sequence[Relation],
    relation_evidence: Sequence[RelationEvidence],
    entity_id_mapping: dict[UUID, UUID],
) -> RelationMergeApplyPlan:
    """Pure planning half of B4's relation-merge algorithm (ADR-0009 SS O):
    read-only in effect, produces a plan carrying only identifiers/numbers/
    quote text destined for a *transient, process-local* staged batch (never
    ``StepResult``) -- see ``pipelines.steps.improve``'s per-step caches.
    ``apply_relation_merge_plan`` is the impure, engine-uow-only twin that
    actually mutates rows and inserts evidence, called from ``persist``.
    """

    plans = [
        _relation_merge_plan(relation, entity_id_mapping)
        for relation in sorted(relations, key=_relation_sort_key)
    ]
    evidence_by_relation = _relation_evidence_by_relation(relation_evidence)

    self_loop_deactivate: list[UUID] = []
    grouped_plans: dict[tuple[UUID, UUID, str, int], list[_RelationMergePlan]] = {}
    for plan in plans:
        if plan.changed and plan.self_loop:
            self_loop_deactivate.append(plan.relation.id)
            continue
        grouped_plans.setdefault(plan.identity, []).append(plan)

    applies: list[RelationRewireApply] = []
    evidence_copies: list[EvidenceCopyApply] = []
    for group in grouped_plans.values():
        if not any(plan.changed for plan in group):
            continue
        survivor_plan = min(group, key=_relation_survivor_plan_sort_key)
        loser_plans = [plan for plan in group if plan.relation.id != survivor_plan.relation.id]
        survivor = survivor_plan.relation

        confidence = float(survivor.confidence)
        importance_weight = float(survivor.importance_weight)
        if loser_plans:
            confidence = max(float(plan.relation.confidence) for plan in group)
            importance_weight = max(float(plan.relation.importance_weight) for plan in group)

        loser_ids: list[UUID] = []
        existing_chunk_ids = {
            evidence.chunk_id for evidence in evidence_by_relation.get(survivor.id, [])
        }
        for loser_plan in loser_plans:
            loser = loser_plan.relation
            if loser.is_active:
                loser_ids.append(loser.id)
            for evidence in evidence_by_relation.get(loser.id, []):
                if evidence.chunk_id in existing_chunk_ids:
                    continue
                evidence_copies.append(
                    EvidenceCopyApply(
                        survivor_relation_id=survivor.id,
                        chunk_id=evidence.chunk_id,
                        quote=evidence.quote,
                        confidence=float(evidence.confidence),
                    )
                )
                existing_chunk_ids.add(evidence.chunk_id)

        if survivor_plan.changed or loser_plans:
            applies.append(
                RelationRewireApply(
                    survivor_relation_id=survivor.id,
                    changed=survivor_plan.changed,
                    source_entity_id=survivor_plan.mapped_source_entity_id,
                    target_entity_id=survivor_plan.mapped_target_entity_id,
                    confidence=confidence,
                    importance_weight=importance_weight,
                    loser_relation_ids=tuple(loser_ids),
                )
            )

    return RelationMergeApplyPlan(
        self_loop_deactivate_ids=tuple(self_loop_deactivate),
        applies=tuple(applies),
        evidence_copies=tuple(evidence_copies),
    )


class _RelationRepositoryForMergeApply(Protocol):
    async def get_by_id(self, relation_id: UUID) -> Relation | None: ...


class _RelationEvidenceRepositoryForMergeApply(Protocol):
    async def add(self, evidence: RelationEvidence) -> RelationEvidence: ...


async def apply_relation_merge_plan(
    *,
    relations_repo: _RelationRepositoryForMergeApply,
    relation_evidence_repo: _RelationEvidenceRepositoryForMergeApply,
    dataset_id: UUID,
    plan: RelationMergeApplyPlan,
) -> tuple[RelationMergeCounts, list[ProjectionCommand]]:
    """Impure application half: mutates freshly-loaded ORM rows attached to
    the caller's *own* (engine-owned) unit of work and inserts evidence.
    Never commits -- the caller (a ``PipelineStep.persist``) owns that.
    """

    rewired = 0
    deactivated = 0
    evidence_copied = 0
    commands: list[ProjectionCommand] = []

    for relation_id in plan.self_loop_deactivate_ids:
        relation = await relations_repo.get_by_id(relation_id)
        if relation is not None and relation.is_active:
            relation.is_active = False
            relation.embedding = None
            deactivated += 1

    for apply in sorted(plan.applies, key=lambda item: item.survivor_relation_id):
        survivor = await relations_repo.get_by_id(apply.survivor_relation_id)
        if survivor is None:
            continue
        if apply.changed:
            survivor.source_entity_id = apply.source_entity_id
            survivor.target_entity_id = apply.target_entity_id
            survivor.embedding = None
            rewired += 1
        if apply.loser_relation_ids:
            survivor.confidence = apply.confidence
            survivor.importance_weight = apply.importance_weight
        for loser_id in apply.loser_relation_ids:
            loser = await relations_repo.get_by_id(loser_id)
            if loser is not None and loser.is_active:
                loser.is_active = False
                loser.embedding = None
                deactivated += 1
        if apply.changed or apply.loser_relation_ids:
            commands.append(
                relation_upsert_command(
                    relation_id=survivor.id,
                    dataset_id=dataset_id,
                    source_entity_id=survivor.source_entity_id,
                    target_entity_id=survivor.target_entity_id,
                    predicate=survivor.predicate,
                    description=survivor.description,
                    confidence=float(survivor.confidence),
                    importance_weight=float(survivor.importance_weight),
                    generation=survivor.generation,
                )
            )

    for copy in plan.evidence_copies:
        await relation_evidence_repo.add(
            RelationEvidence(
                relation_id=copy.survivor_relation_id,
                chunk_id=copy.chunk_id,
                quote=copy.quote,
                confidence=copy.confidence,
            )
        )
        evidence_copied += 1

    return (
        RelationMergeCounts(
            rewired=rewired, deactivated=deactivated, evidence_copied=evidence_copied
        ),
        commands,
    )


def _entity_survivor_sort_key(entity: Entity) -> tuple[datetime, UUID]:
    return (_created_at(entity.created_at), entity.id)


def _relation_sort_key(relation: Relation) -> tuple[datetime, UUID]:
    return (_created_at(relation.created_at), relation.id)


def _created_at(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _relation_merge_plan(
    relation: Relation, entity_id_mapping: dict[UUID, UUID]
) -> _RelationMergePlan:
    mapped_source = entity_id_mapping.get(relation.source_entity_id, relation.source_entity_id)
    mapped_target = entity_id_mapping.get(relation.target_entity_id, relation.target_entity_id)
    changed = (
        mapped_source != relation.source_entity_id or mapped_target != relation.target_entity_id
    )
    return _RelationMergePlan(
        relation=relation,
        mapped_source_entity_id=mapped_source,
        mapped_target_entity_id=mapped_target,
        changed=changed,
        self_loop=mapped_source == mapped_target,
    )


def _relation_survivor_plan_sort_key(plan: _RelationMergePlan) -> tuple[int, datetime, UUID]:
    canonical_endpoint_priority = 1 if plan.changed else 0
    return (canonical_endpoint_priority, *_relation_sort_key(plan.relation))


def _relation_evidence_by_relation(
    evidence_items: Sequence[RelationEvidence],
) -> dict[UUID, list[RelationEvidence]]:
    result: dict[UUID, list[RelationEvidence]] = {}
    for evidence in evidence_items:
        result.setdefault(evidence.relation_id, []).append(evidence)
    return result
