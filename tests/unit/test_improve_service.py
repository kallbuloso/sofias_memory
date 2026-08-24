"""Unit tests for Improve's pure/reusable primitives (SM-511).

Since ``services.improve`` no longer owns run lifecycle (that moved to
``pipelines.steps.improve``, ADR-0009 SS O), this file exercises only the
pure business-logic helpers: feedback-weight math, embedding-text
formatting, entity-merge planning, and relation-merge planning. Step-level
(execute/persist) behavior lives in ``test_improve_pipeline_steps.py``;
route/submission behavior lives in ``test_improve_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import sqrt
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.infrastructure.postgres.models import (
    Entity,
    EntityMention,
    Relation,
    RelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.entities import EntityDuplicateCandidate
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.infrastructure.postgres.repositories.relations import RelationEmbeddingCandidate
from sofias_memory.services.feedback import ANSWER_TARGET_TYPE, REFERENCE_TARGET_TYPE
from sofias_memory.services.improve import (
    DEFAULT_IMPROVE_STAGES,
    SUPPORTED_IMPROVE_STAGES,
    apply_feedback_to_importance,
    apply_relation_merge_plan,
    entity_embedding_text,
    feedback_target_chunk_ids,
    improve_run_input,
    merged_entity_aliases,
    normalize_feedback_score,
    normalize_improve_stages,
    plan_entity_merges,
    plan_relation_merges,
    reassign_entity_mentions,
    relation_embedding_text,
    stream_update_weight,
    validate_embedding_response,
)


def entity(*, generation: int = 0, weight: float = 0.5) -> Entity:
    dataset_id = uuid4()
    return Entity(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        canonical_key=f"concept:{uuid4()}",
        name="PostgreSQL",
        entity_type="Database",
        description="Source of truth.",
        aliases=[],
        properties={},
        confidence=1.0,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )


def relation(
    dataset_id: UUID,
    source_entity_id: UUID,
    target_entity_id: UUID,
    *,
    generation: int = 0,
    weight: float = 0.5,
) -> Relation:
    return Relation(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate="uses",
        description="Uses projection.",
        properties={},
        confidence=0.8,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )


def unit_embedding(cosine_to_first_axis: float) -> list[float]:
    return [cosine_to_first_axis, sqrt(1 - cosine_to_first_axis**2)] + [0.0] * 3070


# ---------------------------------------------------------------------------
# normalize_improve_stages / improve_run_input (SM-511 MAJOR 1: request
# order is preserved, never reordered).
# ---------------------------------------------------------------------------


def test_normalize_improve_stages_defaults_to_default_stages_in_declared_order() -> None:
    assert normalize_improve_stages(None) == list(DEFAULT_IMPROVE_STAGES)


def test_normalize_improve_stages_preserves_explicit_request_order() -> None:
    assert normalize_improve_stages(["relation_embeddings", "feedback_weights"]) == [
        "relation_embeddings",
        "feedback_weights",
    ]
    assert normalize_improve_stages(["feedback_weights", "relation_embeddings"]) == [
        "feedback_weights",
        "relation_embeddings",
    ]


def test_normalize_improve_stages_dedupes_preserving_first_occurrence() -> None:
    assert normalize_improve_stages(
        ["relation_embeddings", "feedback_weights", "relation_embeddings"]
    ) == ["relation_embeddings", "feedback_weights"]


def test_normalize_improve_stages_none_and_explicit_default_order_are_identical() -> None:
    assert normalize_improve_stages(None) == normalize_improve_stages(list(DEFAULT_IMPROVE_STAGES))


def test_normalize_improve_stages_rejects_unsupported_stage() -> None:
    with pytest.raises(SofiasMemoryError):
        normalize_improve_stages(["not_a_real_stage"])


def test_normalize_improve_stages_covers_every_supported_stage() -> None:
    ordered = sorted(SUPPORTED_IMPROVE_STAGES)
    assert normalize_improve_stages(ordered) == ordered


def test_improve_run_input_excludes_wait_from_work_identity() -> None:
    work_input = improve_run_input("main", ["feedback_weights"])
    assert "wait" not in work_input
    assert work_input == {"dataset": "main", "stages": ["feedback_weights"]}


# ---------------------------------------------------------------------------
# Feedback weight math (unchanged B4 algorithm)
# ---------------------------------------------------------------------------


def test_feedback_weight_normalization_and_streaming_formula() -> None:
    assert normalize_feedback_score(-1) == 0.0
    assert normalize_feedback_score(0) == 0.5
    assert normalize_feedback_score(1) == 1.0
    with pytest.raises(ValueError):
        normalize_feedback_score(2)

    assert stream_update_weight(0.5, 1.0, alpha=0.1) == pytest.approx(0.55)
    assert stream_update_weight(0.5, 0.0, alpha=0.1) == pytest.approx(0.45)
    assert stream_update_weight(0.95, 1.0, alpha=0.5) == pytest.approx(0.975)


def test_apply_feedback_to_importance_without_marker_updates_weight_directly() -> None:
    next_weight, next_properties = apply_feedback_to_importance(
        properties={}, current_importance_weight=0.5, normalized_score=1.0
    )
    assert next_weight == pytest.approx(0.55)
    assert next_properties == {}


def test_apply_feedback_to_importance_with_marker_updates_feedback_component_only() -> None:
    properties = {
        "_sofias_memory_importance": {
            "version": "degree-v1",
            "feedback_weight": 0.4,
            "centrality_weight": 0.8,
        }
    }
    next_weight, next_properties = apply_feedback_to_importance(
        properties=properties, current_importance_weight=0.6, normalized_score=1.0
    )
    marker = next_properties["_sofias_memory_importance"]
    assert marker["feedback_weight"] == pytest.approx(0.46)
    assert marker["centrality_weight"] == pytest.approx(0.8)
    assert next_weight == pytest.approx((0.46 + 0.8) / 2.0, abs=1e-4)


@pytest.mark.parametrize("marker", [{"version": "unknown"}, "not-a-dict", 42])
def test_apply_feedback_treats_invalid_importance_marker_as_legacy(marker: object) -> None:
    next_weight, _ = apply_feedback_to_importance(
        properties={"_sofias_memory_importance": marker},
        current_importance_weight=0.5,
        normalized_score=1.0,
    )
    assert next_weight == pytest.approx(0.55)


def test_feedback_target_chunk_ids_resolves_reference_and_answer_targets() -> None:
    reference_chunk = uuid4()
    reference_feedback = UnappliedFeedback(
        id=uuid4(),
        query_id=uuid4(),
        target_type=REFERENCE_TARGET_TYPE,
        target_id=reference_chunk,
        score=1,
        references={},
    )
    assert feedback_target_chunk_ids(reference_feedback) == [reference_chunk]

    chunk_a, chunk_b = sorted([uuid4(), uuid4()])
    answer_feedback = UnappliedFeedback(
        id=uuid4(),
        query_id=uuid4(),
        target_type=ANSWER_TARGET_TYPE,
        target_id=None,
        score=1,
        references={"items": [{"chunk_id": str(chunk_a)}, {"chunk_id": str(chunk_b)}]},
    )
    assert feedback_target_chunk_ids(answer_feedback) == [chunk_a, chunk_b]


# ---------------------------------------------------------------------------
# Embedding text formatting / response validation
# ---------------------------------------------------------------------------


def test_relation_embedding_text_is_deterministic_and_omits_metadata() -> None:
    candidate = RelationEmbeddingCandidate(
        relation_id=uuid4(),
        source_name="  Source  ",
        target_name="  Target  ",
        predicate="  relates_to  ",
        description="  extra detail  ",
    )
    assert relation_embedding_text(candidate) == "Source-›relates_to: extra detail-›Target"


def test_entity_embedding_text_is_stripped_name_only() -> None:
    candidate = EntityEmbeddingCandidateStub(name="  PostgreSQL  ")
    assert entity_embedding_text(candidate) == "PostgreSQL"


class EntityEmbeddingCandidateStub:
    def __init__(self, *, name: str) -> None:
        self.name = name


def test_validate_embedding_response_rejects_count_and_dimension_mismatch() -> None:
    with pytest.raises(DependencyUnavailableError):
        validate_embedding_response(
            [], expected_count=1, expected_dimensions=3072, subject="Entity"
        )
    with pytest.raises(DependencyUnavailableError):
        validate_embedding_response(
            [[0.1] * 10], expected_count=1, expected_dimensions=3072, subject="Entity"
        )
    validate_embedding_response(
        [[0.1] * 3072], expected_count=1, expected_dimensions=3072, subject="Entity"
    )


# ---------------------------------------------------------------------------
# Entity-merge planning (unchanged B4 algorithm)
# ---------------------------------------------------------------------------


def test_plan_entity_merges_picks_earliest_survivor_and_ignores_transitive_pairs() -> None:
    first = entity()
    first.id = UUID("10000000-0000-0000-0000-000000000001")
    first.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    bridge = entity()
    bridge.id = UUID("20000000-0000-0000-0000-000000000002")
    bridge.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    indirect = entity()
    indirect.id = UUID("30000000-0000-0000-0000-000000000003")
    indirect.created_at = datetime(2026, 1, 3, tzinfo=UTC)
    entities_by_id = {first.id: first, bridge.id: bridge, indirect.id: indirect}
    candidates = [
        EntityDuplicateCandidate(
            entity_id=first.id,
            entity_name="a",
            candidate_id=bridge.id,
            candidate_name="b",
            entity_type="concept",
            similarity=0.97,
        ),
        EntityDuplicateCandidate(
            entity_id=bridge.id,
            entity_name="b",
            candidate_id=indirect.id,
            candidate_name="c",
            entity_type="concept",
            similarity=0.93,
        ),
    ]

    merge_plan = plan_entity_merges(entities_by_id, candidates)

    assert merge_plan == {bridge.id: first.id}
    assert indirect.id not in merge_plan


def test_merged_entity_aliases_dedupes_case_insensitively_and_drops_survivor_name() -> None:
    survivor = entity()
    survivor.name = "PostgreSQL"
    survivor.aliases = ["PG"]
    duplicate = entity()
    duplicate.name = "Postgres"
    duplicate.aliases = ["pg", "", "PostgreSQL", "Postgres DB"]

    aliases = merged_entity_aliases(survivor=survivor, duplicate=duplicate)

    assert aliases == ["PG", "Postgres", "Postgres DB"]


def test_reassign_entity_mentions_only_touches_mapped_entities() -> None:
    survivor_id = uuid4()
    duplicate_id = uuid4()
    untouched_id = uuid4()
    mapping = {duplicate_id: survivor_id}
    dataset_id = uuid4()
    mention_a = EntityMention(
        id=uuid4(),
        entity_id=duplicate_id,
        chunk_id=uuid4(),
        surface_text="x",
        start_char=0,
        end_char=1,
        confidence=0.9,
    )
    mention_b = EntityMention(
        id=uuid4(),
        entity_id=untouched_id,
        chunk_id=uuid4(),
        surface_text="y",
        start_char=0,
        end_char=1,
        confidence=0.9,
    )

    commands = reassign_entity_mentions(
        mentions=[mention_a, mention_b], entity_id_mapping=mapping, dataset_id=dataset_id
    )

    assert mention_a.entity_id == survivor_id
    assert mention_b.entity_id == untouched_id
    assert len(commands) == 1
    assert commands[0].aggregate_id == str(mention_a.id)


# ---------------------------------------------------------------------------
# Relation-merge planning + application (SM-511 pure/impure split)
# ---------------------------------------------------------------------------


def test_plan_relation_merges_rewires_survivor_and_deactivates_loser_with_evidence_copy() -> None:
    dataset_id = uuid4()
    survivor_entity = uuid4()
    duplicate_entity = uuid4()
    target_entity = uuid4()
    canonical_relation = relation(dataset_id, survivor_entity, target_entity, weight=0.4)
    canonical_relation.confidence = 0.6
    canonical_relation.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    duplicate_relation = relation(dataset_id, duplicate_entity, target_entity, weight=0.9)
    duplicate_relation.confidence = 0.8
    duplicate_relation.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    existing_chunk = uuid4()
    new_chunk = uuid4()
    evidence = [
        RelationEvidence(
            relation_id=canonical_relation.id,
            chunk_id=existing_chunk,
            quote="existing evidence",
            confidence=0.7,
        ),
        RelationEvidence(
            relation_id=duplicate_relation.id,
            chunk_id=existing_chunk,
            quote="do not overwrite",
            confidence=0.9,
        ),
        RelationEvidence(
            relation_id=duplicate_relation.id,
            chunk_id=new_chunk,
            quote="new evidence",
            confidence=0.8,
        ),
    ]

    plan = plan_relation_merges(
        relations=[canonical_relation, duplicate_relation],
        relation_evidence=evidence,
        entity_id_mapping={duplicate_entity: survivor_entity},
    )

    assert len(plan.applies) == 1
    apply = plan.applies[0]
    assert apply.survivor_relation_id == canonical_relation.id
    assert apply.loser_relation_ids == (duplicate_relation.id,)
    assert apply.confidence == pytest.approx(0.8)
    assert apply.importance_weight == pytest.approx(0.9)
    assert len(plan.evidence_copies) == 1
    assert plan.evidence_copies[0].chunk_id == new_chunk
    assert plan.evidence_copies[0].quote == "new evidence"


def test_plan_relation_merges_deactivates_self_loop_without_evidence_copy() -> None:
    dataset_id = uuid4()
    survivor_entity = uuid4()
    duplicate_entity = uuid4()
    self_loop = relation(dataset_id, duplicate_entity, survivor_entity)

    plan = plan_relation_merges(
        relations=[self_loop],
        relation_evidence=[],
        entity_id_mapping={duplicate_entity: survivor_entity},
    )

    assert plan.self_loop_deactivate_ids == (self_loop.id,)
    assert plan.applies == ()
    assert plan.evidence_copies == ()


@pytest.mark.asyncio
async def test_apply_relation_merge_plan_mutates_only_planned_relations_once() -> None:
    dataset_id = uuid4()
    survivor_entity = uuid4()
    duplicate_entity = uuid4()
    target_entity = uuid4()
    survivor = relation(dataset_id, survivor_entity, target_entity, weight=0.4)
    survivor.confidence = 0.6
    loser = relation(dataset_id, duplicate_entity, target_entity, weight=0.9)
    loser.confidence = 0.8
    self_loop = relation(dataset_id, duplicate_entity, survivor_entity)

    plan = plan_relation_merges(
        relations=[survivor, loser, self_loop],
        relation_evidence=[],
        entity_id_mapping={duplicate_entity: survivor_entity},
    )

    class FakeRelationsRepo:
        def __init__(self) -> None:
            self._by_id = {r.id: r for r in (survivor, loser, self_loop)}

        async def get_by_id(self, relation_id: UUID) -> Relation | None:
            return self._by_id.get(relation_id)

    class FakeEvidenceRepo:
        def __init__(self) -> None:
            self.added: list[RelationEvidence] = []

        async def add(self, evidence: RelationEvidence) -> RelationEvidence:
            self.added.append(evidence)
            return evidence

    relations_repo = FakeRelationsRepo()
    evidence_repo = FakeEvidenceRepo()

    counts, commands = await apply_relation_merge_plan(
        relations_repo=relations_repo,
        relation_evidence_repo=evidence_repo,
        dataset_id=dataset_id,
        plan=plan,
    )

    assert counts.rewired == 0
    assert counts.deactivated == 2
    assert loser.is_active is False
    assert self_loop.is_active is False
    assert survivor.is_active is True
    assert survivor.confidence == pytest.approx(0.8)
    assert survivor.importance_weight == pytest.approx(0.9)
    assert len(commands) == 1
    assert commands[0].aggregate_id == str(survivor.id)
