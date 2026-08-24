"""Unit tests for the Improve B5 pipeline steps (SM-511, ADR-0009 SS O).

Covers the fixed-slot dispatch mechanism (SM-511 MAJOR 1: request stage
order is preserved, not canonicalized) and the steps whose ``execute``/
``persist`` boundary is fully testable without a real PostgreSQL session:
the four "everything lives in persist" handlers (feedback_weights,
entity_merge, graph_maintain, finalize_result) and the Neo4j-facing handlers
that only touch injected ``resources`` (graph_reconcile, graph_drain,
final_convergence) plus the staged-batch ``persist`` half of the two
embedding steps. ``execute`` for the embedding/summaries handlers opens a
real ``PostgresUnitOfWork`` internally and is exercised by the integration
suite instead (SM-511 self-audit: documented, not silently skipped).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import (
    Dataset,
    Entity,
    EntityMention,
    PipelineRun,
    Relation,
    RelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.entities import EntityDuplicateCandidate
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.pipelines.registry import StepResult
from sofias_memory.pipelines.steps.improve import (
    MAIN_PHASE,
    POST_PHASE,
    PRE_PHASE,
    EntityEmbeddingsStep,
    EntityMergeStep,
    FeedbackWeightsStep,
    FinalConvergenceStep,
    FinalizeResultStep,
    GraphDrainStep,
    GraphMaintainStep,
    GraphReconcileStep,
    ImprovePipelineResources,
    RelationEmbeddingsStep,
    SlotStep,
    build_improve_pipeline_definition,
    resolve_slot_stages,
    slot_step_name,
)
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxDrainResult
from sofias_memory.services.graph_reconciliation_service import (
    GraphReconciliationDiff,
    GraphReconciliationResult,
)


def entity(*, dataset_id: UUID, weight: float = 0.5, generation: int = 0) -> Entity:
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
    dataset_id: UUID, source_entity_id: UUID, target_entity_id: UUID, *, weight: float = 0.5
) -> Relation:
    return Relation(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=0,
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


def make_context(
    *,
    dataset_id: UUID | None,
    stages: list[str],
    run_id: UUID | None = None,
    resources: object | None = None,
    step_outputs: dict[str, dict[str, object]] | None = None,
) -> PipelineContext:
    return PipelineContext(
        run_id=run_id or uuid4(),
        pipeline_type=PipelineType.IMPROVE,
        dataset_id=dataset_id,
        source_id=None,
        run_input={"dataset": "main", "stages": stages},
        step_outputs=step_outputs or {},
        session_factory=None,  # type: ignore[arg-type] - unused by these steps
        resources={"improve_resources": resources} if resources is not None else {},
    )


class _RecordingUow:
    def __init__(self) -> None:
        self.calls: list[str] = []


# ---------------------------------------------------------------------------
# resolve_slot_stages / SlotStep dispatch (SM-511 MAJOR 1)
# ---------------------------------------------------------------------------


def test_resolve_slot_stages_pads_and_preserves_request_order() -> None:
    run_input = {"stages": ["relation_embeddings", "feedback_weights"]}
    assert resolve_slot_stages(run_input) == [
        "relation_embeddings",
        "feedback_weights",
        None,
        None,
        None,
    ]


def test_resolve_slot_stages_keeps_graph_reconciliation_at_its_public_position() -> None:
    """SM-511 MAJOR 1 item E: a compound stage in the middle of the request
    keeps its public list position; its three internal phases (pre/main/
    post) all resolve to that SAME slot index, regardless of where it sits."""

    stages = resolve_slot_stages(
        {"stages": ["relation_embeddings", "graph_reconciliation", "feedback_weights"]}
    )
    assert stages == ["relation_embeddings", "graph_reconciliation", "feedback_weights", None, None]
    assert stages.index("graph_reconciliation") == 1


def test_resolve_slot_stages_reversed_request_is_a_different_slot_assignment() -> None:
    forward = resolve_slot_stages({"stages": ["feedback_weights", "relation_embeddings"]})
    backward = resolve_slot_stages({"stages": ["relation_embeddings", "feedback_weights"]})
    assert forward != backward
    assert forward[0] == "feedback_weights"
    assert backward[0] == "relation_embeddings"


@pytest.mark.asyncio
async def test_slot_step_is_a_true_no_op_when_no_stage_occupies_the_slot() -> None:
    context = make_context(dataset_id=uuid4(), stages=["feedback_weights"])
    slot = SlotStep(1, MAIN_PHASE)  # only slot 0 is occupied

    result = await slot.execute(context)
    assert result.output == {}
    recorder = _RecordingUow()
    await slot.persist(context, result, recorder)  # type: ignore[arg-type]
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_slot_step_dispatches_to_the_handler_at_its_position() -> None:
    dataset_id = uuid4()
    e1 = entity(dataset_id=dataset_id)
    e2 = entity(dataset_id=dataset_id)
    r = relation(dataset_id, e1.id, e2.id)
    dataset = Dataset(
        id=dataset_id,
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    uow = FakeMaintainUow(dataset=dataset, entities=[e1, e2], relations=[r])
    # graph_reconciliation occupies slot 0 here -- its "main" phase is graph_maintain.
    context = make_context(dataset_id=dataset_id, stages=["graph_reconciliation"])
    slot = SlotStep(0, MAIN_PHASE)

    result = await slot.execute(context)
    await slot.persist(context, result, uow)  # type: ignore[arg-type]

    assert result.output["entities_importance_updated"] == 2


# ---------------------------------------------------------------------------
# feedback_weights (main phase handler)
# ---------------------------------------------------------------------------


class FakeFeedbackRepo:
    def __init__(self, items: list[UnappliedFeedback]) -> None:
        self._items = items
        self.applied_ids: list[UUID] = []

    async def list_unapplied_for_dataset(self, dataset_id: UUID) -> list[UnappliedFeedback]:
        return list(self._items)

    async def mark_applied(self, feedback_id: UUID, *, applied_at: datetime) -> object | None:
        self.applied_ids.append(feedback_id)
        return None


class FakeEntityMentionsRepo:
    def __init__(self, by_chunk: dict[UUID, list[Entity]]) -> None:
        self._by_chunk = by_chunk

    async def list_active_entities_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Entity]:
        seen: dict[UUID, Entity] = {}
        for chunk_id in chunk_ids:
            for item in self._by_chunk.get(chunk_id, []):
                seen[item.id] = item
        return list(seen.values())

    async def list_for_entities(self, *, entity_ids: list[UUID]) -> list[EntityMention]:
        raise NotImplementedError


class FakeRelationEvidenceRepo:
    def __init__(
        self,
        by_chunk: dict[UUID, list[Relation]] | None = None,
        evidence: list[RelationEvidence] | None = None,
    ) -> None:
        self._by_chunk = by_chunk or {}
        self.evidence = evidence or []
        self.added: list[RelationEvidence] = []

    async def list_active_relations_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Relation]:
        seen: dict[UUID, Relation] = {}
        for chunk_id in chunk_ids:
            for item in self._by_chunk.get(chunk_id, []):
                seen[item.id] = item
        return list(seen.values())

    async def list_for_relations(self, *, relation_ids: list[UUID]) -> list[RelationEvidence]:
        return [e for e in self.evidence if e.relation_id in relation_ids]

    async def add(self, evidence: RelationEvidence) -> RelationEvidence:
        self.added.append(evidence)
        return evidence


class FakeGraphOutboxRepo:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def add_projection_command(self, command: object) -> object:
        self.commands.append(command)
        return command


class FakeFeedbackUow:
    def __init__(
        self,
        *,
        feedback: FakeFeedbackRepo,
        entity_mentions: FakeEntityMentionsRepo,
        relation_evidence: FakeRelationEvidenceRepo,
        graph_outbox: FakeGraphOutboxRepo,
    ) -> None:
        self.feedback = feedback
        self.entity_mentions = entity_mentions
        self.relation_evidence = relation_evidence
        self.graph_outbox = graph_outbox


@pytest.mark.asyncio
async def test_feedback_weights_persist_applies_weights_and_marks_all_processed_feedback() -> None:
    dataset_id = uuid4()
    chunk_id = uuid4()
    target_entity = entity(dataset_id=dataset_id)
    feedback_with_target = UnappliedFeedback(
        id=uuid4(),
        query_id=uuid4(),
        target_type="reference",
        target_id=chunk_id,
        score=1,
        references={},
    )
    feedback_without_target = UnappliedFeedback(
        id=uuid4(),
        query_id=uuid4(),
        target_type="reference",
        target_id=uuid4(),
        score=1,
        references={},
    )
    uow = FakeFeedbackUow(
        feedback=FakeFeedbackRepo([feedback_with_target, feedback_without_target]),
        entity_mentions=FakeEntityMentionsRepo({chunk_id: [target_entity]}),
        relation_evidence=FakeRelationEvidenceRepo(),
        graph_outbox=FakeGraphOutboxRepo(),
    )
    context = make_context(dataset_id=dataset_id, stages=["feedback_weights"])
    step = FeedbackWeightsStep()

    exec_result = await step.execute(context)
    await step.persist(context, exec_result, uow)  # type: ignore[arg-type]

    assert exec_result.output["applied"] == 1
    assert exec_result.output["skipped"] == 1
    assert exec_result.output["entities_updated"] == 1
    assert exec_result.output["graph_events_enqueued"] == 1
    assert set(uow.feedback.applied_ids) == {feedback_with_target.id, feedback_without_target.id}
    assert target_entity.importance_weight == pytest.approx(0.55)
    assert len(uow.graph_outbox.commands) == 1


# ---------------------------------------------------------------------------
# entity_merge (main phase handler)
# ---------------------------------------------------------------------------


class FakeEntitiesRepo:
    def __init__(self, entities: list[Entity], candidates: list[EntityDuplicateCandidate]) -> None:
        self._entities = {e.id: e for e in entities}
        self._candidates = candidates

    async def list_duplicate_candidates(
        self, *, dataset_id: UUID, similarity_threshold: float
    ) -> list[EntityDuplicateCandidate]:
        return [c for c in self._candidates if c.similarity >= similarity_threshold]

    async def list_active_current_by_ids(
        self, *, dataset_id: UUID, entity_ids: list[UUID]
    ) -> list[Entity]:
        return [self._entities[eid] for eid in entity_ids if eid in self._entities]


class FakeEntityMentionsForMergeRepo:
    def __init__(self, mentions: list[EntityMention]) -> None:
        self._mentions = mentions

    async def list_for_entities(self, *, entity_ids: list[UUID]) -> list[EntityMention]:
        return [m for m in self._mentions if m.entity_id in entity_ids]


class FakeRelationsForMergeRepo:
    def __init__(self, relations: list[Relation]) -> None:
        self._relations = {r.id: r for r in relations}

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Relation]:
        return list(self._relations.values())

    async def get_by_id(self, relation_id: UUID) -> Relation | None:
        return self._relations.get(relation_id)


class FakeEntityMergeUow:
    def __init__(
        self,
        *,
        entities: FakeEntitiesRepo,
        entity_mentions: FakeEntityMentionsForMergeRepo,
        relations: FakeRelationsForMergeRepo,
        relation_evidence: FakeRelationEvidenceRepo,
        graph_outbox: FakeGraphOutboxRepo,
    ) -> None:
        self.entities = entities
        self.entity_mentions = entity_mentions
        self.relations = relations
        self.relation_evidence = relation_evidence
        self.graph_outbox = graph_outbox


class _FakeMergeSettings:
    entity_dedup_similarity_threshold = 0.85
    entity_merge_similarity_threshold = 0.95


@pytest.mark.asyncio
async def test_entity_merge_persist_merges_safe_pair() -> None:
    dataset_id = uuid4()
    survivor = entity(dataset_id=dataset_id)
    survivor.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    duplicate = entity(dataset_id=dataset_id)
    duplicate.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    candidates = [
        EntityDuplicateCandidate(
            entity_id=survivor.id,
            entity_name=survivor.name,
            candidate_id=duplicate.id,
            candidate_name=duplicate.name,
            entity_type="concept",
            similarity=0.99,
        )
    ]
    mention = EntityMention(
        id=uuid4(),
        entity_id=duplicate.id,
        chunk_id=uuid4(),
        surface_text="x",
        start_char=0,
        end_char=1,
        confidence=0.9,
    )
    uow = FakeEntityMergeUow(
        entities=FakeEntitiesRepo([survivor, duplicate], candidates),
        entity_mentions=FakeEntityMentionsForMergeRepo([mention]),
        relations=FakeRelationsForMergeRepo([]),
        relation_evidence=FakeRelationEvidenceRepo(),
        graph_outbox=FakeGraphOutboxRepo(),
    )
    resources = ImprovePipelineResources(
        settings=_FakeMergeSettings(),  # type: ignore[arg-type]
        embedding_client=None,  # type: ignore[arg-type]
        graph_maintenance=None,  # type: ignore[arg-type]
        summary_rebuild=None,  # type: ignore[arg-type]
        graph_reconciliation=None,
        graph_outbox_drain=None,
    )
    context = make_context(
        dataset_id=dataset_id, stages=["entity_deduplication"], resources=resources
    )
    step = EntityMergeStep()

    exec_result = await step.execute(context)
    await step.persist(context, exec_result, uow)  # type: ignore[arg-type]

    assert exec_result.output["entities_merged"] == 1
    assert duplicate.is_active is False
    assert mention.entity_id == survivor.id
    assert exec_result.output["duplicate_candidates"] == 1


# ---------------------------------------------------------------------------
# entity_embeddings / relation_embeddings -- persist half (staged batch)
# ---------------------------------------------------------------------------


class FakeEntitiesEmbedUow:
    def __init__(self) -> None:
        self.calls: list[dict[UUID, list[float]]] = []

    class _Entities:
        def __init__(self, outer: FakeEntitiesEmbedUow) -> None:
            self._outer = outer

        async def set_missing_embeddings_for_active_current(
            self, *, dataset_id: UUID, embeddings_by_entity_id: dict[UUID, list[float]]
        ) -> int:
            self._outer.calls.append(embeddings_by_entity_id)
            return len(embeddings_by_entity_id)

    @property
    def entities(self) -> FakeEntitiesEmbedUow._Entities:
        return FakeEntitiesEmbedUow._Entities(self)


@pytest.mark.asyncio
async def test_entity_embeddings_persist_applies_staged_batch_once() -> None:
    step = EntityEmbeddingsStep()
    context = make_context(dataset_id=uuid4(), stages=["entity_deduplication"])
    entity_id = uuid4()
    step._staged.stage(context.run_id, {entity_id: [0.1, 0.2]})
    result = StepResult(output={"entities_embedded": 1})
    uow = FakeEntitiesEmbedUow()

    await step.persist(context, result, uow)  # type: ignore[arg-type]

    assert uow.calls == [{entity_id: [0.1, 0.2]}]
    assert step._staged.pop(context.run_id) is None  # popped, not left staged


@pytest.mark.asyncio
async def test_entity_embeddings_persist_raises_when_batch_missing() -> None:
    step = EntityEmbeddingsStep()
    context = make_context(dataset_id=uuid4(), stages=["entity_deduplication"])
    result = StepResult(output={})

    with pytest.raises(PermanentPipelineStepError):
        await step.persist(context, result, FakeEntitiesEmbedUow())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_relation_embeddings_persist_applies_staged_batch() -> None:
    class FakeRelationsEmbedUow:
        def __init__(self) -> None:
            self.calls: list[dict[UUID, list[float]]] = []

        class _Relations:
            def __init__(self, outer) -> None:
                self._outer = outer

            async def set_missing_embeddings_for_active_current(
                self, *, dataset_id: UUID, embeddings_by_relation_id: dict[UUID, list[float]]
            ) -> int:
                self._outer.calls.append(embeddings_by_relation_id)
                return len(embeddings_by_relation_id)

        @property
        def relations(self):
            return FakeRelationsEmbedUow._Relations(self)

    step = RelationEmbeddingsStep()
    context = make_context(dataset_id=uuid4(), stages=["relation_embeddings"])
    relation_id = uuid4()
    step._staged.stage(context.run_id, {relation_id: [0.3]})
    result = StepResult(output={"relations_embedded": 1})
    uow = FakeRelationsEmbedUow()

    await step.persist(context, result, uow)  # type: ignore[arg-type]

    assert uow.calls == [{relation_id: [0.3]}]


# ---------------------------------------------------------------------------
# graph_maintain (main phase handler)
# ---------------------------------------------------------------------------


class FakeMaintainDatasetsRepo:
    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return self._dataset if self._dataset.id == dataset_id else None


class FakeMaintainEntitiesRepo:
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Entity]:
        return list(self._entities)

    async def get_active_current_by_id(self, *, dataset_id: UUID, entity_id: UUID) -> Entity | None:
        return next((e for e in self._entities if e.id == entity_id), None)


class FakeMaintainRelationsRepo:
    def __init__(self, relations: list[Relation]) -> None:
        self._relations = relations

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Relation]:
        return list(self._relations)

    async def get_active_current_by_id(
        self, *, dataset_id: UUID, relation_id: UUID
    ) -> Relation | None:
        return next((r for r in self._relations if r.id == relation_id), None)


class FakeRelationEvidenceForMaintainRepo:
    async def list_relation_ids_with_authoritative_evidence(
        self, *, dataset_id: UUID, relation_ids: list[UUID]
    ) -> set[UUID]:
        return set(relation_ids)


class FakeMaintainUow:
    def __init__(
        self,
        *,
        dataset: Dataset,
        entities: list[Entity],
        relations: list[Relation],
    ) -> None:
        self.datasets = FakeMaintainDatasetsRepo(dataset)
        self.entities = FakeMaintainEntitiesRepo(entities)
        self.relations = FakeMaintainRelationsRepo(relations)
        self.relation_evidence = FakeRelationEvidenceForMaintainRepo()
        self.graph_outbox = FakeGraphOutboxRepo()


@pytest.mark.asyncio
async def test_graph_maintain_persist_computes_importance_for_active_dataset() -> None:
    dataset_id = uuid4()
    dataset = Dataset(
        id=dataset_id,
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    e1 = entity(dataset_id=dataset_id)
    e2 = entity(dataset_id=dataset_id)
    r = relation(dataset_id, e1.id, e2.id)
    uow = FakeMaintainUow(dataset=dataset, entities=[e1, e2], relations=[r])
    context = make_context(dataset_id=dataset_id, stages=["graph_reconciliation"])
    step = GraphMaintainStep()

    exec_result = await step.execute(context)
    assert exec_result.output == {}
    await step.persist(context, exec_result, uow)  # type: ignore[arg-type]

    assert exec_result.output["entities_importance_updated"] == 2
    assert "_sofias_memory_importance" in e1.properties


# ---------------------------------------------------------------------------
# graph_reconcile / graph_drain / final_convergence -- fake resources
# ---------------------------------------------------------------------------


class FakeGraphReconciliation:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def reconcile_dataset(self, dataset_id: UUID) -> GraphReconciliationResult:
        self.calls.append(dataset_id)
        return GraphReconciliationResult(diff=GraphReconciliationDiff(), rebuilt=False)


class FakeGraphOutboxDrain:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def process_dataset(self, dataset_id: UUID) -> GraphOutboxDrainResult:
        self.calls.append(dataset_id)
        return GraphOutboxDrainResult(dataset_id=dataset_id, processed=2)


@pytest.mark.asyncio
async def test_graph_reconcile_drains_before_reconciling() -> None:
    order: list[str] = []

    class OrderedDrain(FakeGraphOutboxDrain):
        async def process_dataset(self, dataset_id: UUID) -> GraphOutboxDrainResult:
            order.append("drain")
            return await super().process_dataset(dataset_id)

    class OrderedReconciliation(FakeGraphReconciliation):
        async def reconcile_dataset(self, dataset_id: UUID) -> GraphReconciliationResult:
            order.append("reconcile")
            return await super().reconcile_dataset(dataset_id)

    resources = ImprovePipelineResources(
        settings=None,  # type: ignore[arg-type]
        embedding_client=None,  # type: ignore[arg-type]
        graph_maintenance=None,  # type: ignore[arg-type]
        summary_rebuild=None,  # type: ignore[arg-type]
        graph_reconciliation=OrderedReconciliation(),  # type: ignore[arg-type]
        graph_outbox_drain=OrderedDrain(),  # type: ignore[arg-type]
    )
    dataset_id = uuid4()
    context = make_context(
        dataset_id=dataset_id, stages=["graph_reconciliation"], resources=resources
    )

    result = await GraphReconcileStep().execute(context)

    assert order == ["drain", "reconcile"]
    assert result.output["graph_events_processed_pre_drain"] == 2


@pytest.mark.asyncio
async def test_graph_drain_reports_processed_count() -> None:
    drain = FakeGraphOutboxDrain()
    resources = ImprovePipelineResources(
        settings=None,  # type: ignore[arg-type]
        embedding_client=None,  # type: ignore[arg-type]
        graph_maintenance=None,  # type: ignore[arg-type]
        summary_rebuild=None,  # type: ignore[arg-type]
        graph_reconciliation=None,
        graph_outbox_drain=drain,  # type: ignore[arg-type]
    )
    dataset_id = uuid4()
    context = make_context(
        dataset_id=dataset_id, stages=["graph_reconciliation"], resources=resources
    )

    result = await GraphDrainStep().execute(context)

    assert result.output["graph_events_processed"] == 2
    assert drain.calls == [dataset_id]


@pytest.mark.asyncio
async def test_final_convergence_always_drains_regardless_of_selected_stages() -> None:
    """SM-511 MAJOR 2: converges even when graph_reconciliation was never
    requested -- feedback_weights/entity_merge can still enqueue events."""

    drain = FakeGraphOutboxDrain()
    resources = ImprovePipelineResources(
        settings=None,  # type: ignore[arg-type]
        embedding_client=None,  # type: ignore[arg-type]
        graph_maintenance=None,  # type: ignore[arg-type]
        summary_rebuild=None,  # type: ignore[arg-type]
        graph_reconciliation=None,
        graph_outbox_drain=drain,  # type: ignore[arg-type]
    )
    dataset_id = uuid4()
    context = make_context(dataset_id=dataset_id, stages=["feedback_weights"], resources=resources)

    result = await FinalConvergenceStep().execute(context)

    assert result.output["graph_events_processed"] == 2
    assert drain.calls == [dataset_id]


@pytest.mark.asyncio
async def test_final_convergence_degrades_to_no_op_without_neo4j_resource() -> None:
    resources = ImprovePipelineResources(
        settings=None,  # type: ignore[arg-type]
        embedding_client=None,  # type: ignore[arg-type]
        graph_maintenance=None,  # type: ignore[arg-type]
        summary_rebuild=None,  # type: ignore[arg-type]
        graph_reconciliation=None,
        graph_outbox_drain=None,
    )
    context = make_context(dataset_id=uuid4(), stages=["feedback_weights"], resources=resources)

    result = await FinalConvergenceStep().execute(context)

    assert result.output == {"graph_events_processed": 0}


# ---------------------------------------------------------------------------
# finalize_result -- looks up outputs by re-resolved slot index
# ---------------------------------------------------------------------------


class FakeFinalizeDatasetsRepo:
    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return self._dataset


class FakeFinalizeRunRepo:
    def __init__(self, run: PipelineRun) -> None:
        self._run = run

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        return self._run


class FakeFinalizeUow:
    def __init__(self, *, dataset: Dataset, run: PipelineRun) -> None:
        self.datasets = FakeFinalizeDatasetsRepo(dataset)
        self.pipeline_runs = FakeFinalizeRunRepo(run)


@pytest.mark.asyncio
async def test_finalize_result_aggregates_step_outputs_via_reordered_slots() -> None:
    dataset_id = uuid4()
    dataset = Dataset(
        id=dataset_id,
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=3,
    )
    run = PipelineRun(
        id=uuid4(),
        pipeline_type=PipelineType.IMPROVE,
        dataset_id=None,
        source_id=None,
        status=PipelineRunStatus.RUNNING,
        idempotency_key=None,
        payload_hash="h",
        input={},
        progress=0.0,
        current_step="finalize_result",
        attempt=1,
        worker_id="w",
        heartbeat_at=None,
        config_fingerprint="cf",
        error_code=None,
        error_message=None,
        metrics={},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=None,
    )
    uow = FakeFinalizeUow(dataset=dataset, run=run)
    # relation_embeddings requested BEFORE feedback_weights -- slot 0 is
    # relation_embeddings, slot 1 is feedback_weights.
    stages = ["relation_embeddings", "feedback_weights"]
    step_outputs = {
        slot_step_name(0, MAIN_PHASE): {"relations_embedded": 4},
        slot_step_name(1, MAIN_PHASE): {
            "processed": 2,
            "applied": 1,
            "skipped": 1,
            "graph_events_enqueued": 1,
        },
        "final_convergence": {"graph_events_processed": 3},
    }
    context = make_context(
        dataset_id=dataset_id, stages=stages, run_id=run.id, step_outputs=step_outputs
    )

    step = FinalizeResultStep()
    exec_result = await step.execute(context)
    await step.persist(context, exec_result, uow)  # type: ignore[arg-type]

    persisted = run.metrics["improve_result"]
    assert persisted["stages"] == stages
    assert persisted["relations_embedded"] == 4
    assert persisted["feedback_processed"] == 2
    assert persisted["graph_events_enqueued"] == 1
    assert persisted["graph_events_processed"] == 3
    assert run.dataset_id == dataset_id


def test_build_improve_pipeline_definition_has_seventeen_fixed_steps() -> None:
    definition = build_improve_pipeline_definition()

    assert len(definition.steps) == 5 * 3 + 2
    names = [step.name for step in definition.steps]
    assert names[-2:] == ["final_convergence", "finalize_result"]
    assert names[0:3] == [
        slot_step_name(0, PRE_PHASE),
        slot_step_name(0, MAIN_PHASE),
        slot_step_name(0, POST_PHASE),
    ]
    assert len(set(names)) == len(names)  # unique step names
