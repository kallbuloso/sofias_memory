"""SM-CTX-006 ContextualRecallService unit proof (handoff 14 SS 117-124/135-138):
pure deterministic composition plus orchestration over a fake unit of work.
The real SQL eligibility is proven by the PostgreSQL integration suite."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CognitiveMemoryType,
    EvidenceAvailability,
    EvidenceSourceType,
    SupportMode,
)
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointProvenanceResult,
    ContextCheckpointResult,
    ContextStatementResult,
)
from sofias_memory.schemas.context_recall import (
    ContextRecallEpisode,
    ContextRecallLimits,
    ContextRecallRequest,
)
from sofias_memory.schemas.contextual_evidence import EvidenceResult, SupportResult
from sofias_memory.schemas.episodes import EpisodeProvenanceResult, EpisodeResult
from sofias_memory.schemas.memories import MemoryItemResult, MemoryProvenanceResult
from sofias_memory.services import contextual_recall as module
from sofias_memory.services.contextual_recall import (
    ContextualRecallService,
    EpisodeCandidate,
    MemoryCandidate,
    SourceSnapshot,
    aggregate_evidence,
    compose_bundle,
    select_current_checkpoint,
    select_episodes,
    violates_forget_invariant,
)

DIM = 3072
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
A = EvidenceAvailability.AVAILABLE
U = EvidenceAvailability.UNAVAILABLE
F = EvidenceAvailability.FORGOTTEN


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="postgresql+asyncpg://unused:unused@postgres:5432/unused",
        neo4j_password="unused-neo4j-password",
        llm_api_key="sk-fake-test-key",
        cognitive_idempotency_hmac_key="test-cognitive-idempotency-hmac-key-0123456789abcdef",
        app_env="test",
    )


def request(**overrides: Any) -> ContextRecallRequest:
    value: dict[str, Any] = {"scope": "project:x", "intent": "resume", "query": "Onde paramos?"}
    value.update(overrides)
    return ContextRecallRequest.model_validate(value)


def evidence(source_type: EvidenceSourceType, availability: EvidenceAvailability) -> EvidenceResult:
    return EvidenceResult(
        evidence_id=uuid4(), source_type=source_type, source_id=uuid4(), availability=availability
    )


def sup(mode: SupportMode, *items: EvidenceResult) -> SupportResult:
    return SupportResult(mode=mode, evidence=list(items))


def stmt(text: str, support: SupportResult | None = None) -> ContextStatementResult:
    return ContextStatementResult(statement_id=uuid4(), text=text, support=support)


def checkpoint(**overrides: Any) -> ContextCheckpointResult:
    value: dict[str, Any] = {
        "checkpoint_id": uuid4(),
        "scope": "project:x",
        "context_key": "main",
        "revision": 1,
        "lifecycle": "active",
        "objective": stmt("objetivo"),
        "current_state": stmt("estado atual"),
        "approved_decisions": [stmt("decisão")],
        "pending_work": [stmt("pendência 1"), stmt("pendência 2")],
        "open_items": [],
        "blockers": [],
        "next_action": stmt("próxima ação"),
        "provenance": ContextCheckpointProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="test",
            conversation_ref=None,
            turn_ref=None,
        ),
        "created_at": NOW,
        "superseded_at": None,
        "superseded_by": None,
        "invalidated_at": None,
        "forgotten_at": None,
    }
    value.update(overrides)
    return ContextCheckpointResult(**value)


def episode(
    *, support: SupportResult | None = None, minutes: int = 0, episode_id: UUID | None = None
) -> EpisodeResult:
    return EpisodeResult(
        episode_id=episode_id or uuid4(),
        scope="project:x",
        context_key=None,
        kind="decision",  # type: ignore[arg-type]
        summary="resumo",
        situation=None,
        reason=None,
        outcome=None,
        occurred_at=NOW - timedelta(minutes=minutes),
        lifecycle="active",  # type: ignore[arg-type]
        confidence=None,
        provenance=EpisodeProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="test",
            conversation_ref=None,
            turn_ref=None,
        ),
        support=support,
        created_at=NOW,
        invalidated_at=None,
        forgotten_at=None,
    )


def memory() -> MemoryItemResult:
    return MemoryItemResult(
        memory_id=uuid4(),
        memory_type="semantic",  # type: ignore[arg-type]
        scope="project:x",
        content="fato",
        lifecycle="active",  # type: ignore[arg-type]
        confidence=None,
        valid_from=None,
        valid_until=None,
        created_at=NOW,
        superseded_at=None,
        superseded_by=None,
        forgotten_at=None,
        provenance=MemoryProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="test",
            conversation_uuid=None,
            turn_uuid=None,
            task_uuid=None,
            confirmation_ref=None,
            source_ref="ref",
            observed_at=None,
        ),
    )


def snapshot(
    checkpoints: Sequence[ContextCheckpointResult] = (),
    memories: Sequence[MemoryCandidate] = (),
    episodes: Sequence[EpisodeCandidate] = (),
) -> SourceSnapshot:
    return SourceSnapshot(
        active_checkpoints=list(checkpoints), memories=list(memories), episodes=list(episodes)
    )


def signals() -> Any:
    return module._Signals()


# --- support / invariant ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "availabilities", "violation"),
    [
        (SupportMode.ALL_REQUIRED, [F, A], True),
        (SupportMode.ALL_REQUIRED, [U, A], False),  # ordinary invalidation: no propagation
        (SupportMode.ANY_SUFFICIENT, [F, F], True),
        (SupportMode.ANY_SUFFICIENT, [F, U], False),
        (SupportMode.ANY_SUFFICIENT, [F, A], False),
    ],
)
def test_forget_invariant_counts_only_forgotten_losses(
    mode: SupportMode, availabilities: list[EvidenceAvailability], violation: bool
) -> None:
    support = sup(mode, *(evidence(EvidenceSourceType.EPISODE, a) for a in availabilities))
    assert violates_forget_invariant(support) is violation


# --- current checkpoint ----------------------------------------------------------------------


def test_no_active_checkpoint_means_no_continuity_and_no_fallback() -> None:
    s = signals()
    assert select_current_checkpoint([], s) is None
    assert s.warnings == set()


def test_single_active_checkpoint_is_returned_classified() -> None:
    s = signals()
    cp = checkpoint()
    result = select_current_checkpoint([cp], s)
    assert result is not None
    assert result.result_class == "current_checkpoint"
    assert result.checkpoint_id == cp.checkpoint_id
    assert result.next_action == cp.next_action  # context only, never executed
    assert s.warnings == set()


def test_two_active_checkpoints_fail_closed_never_newest_wins() -> None:
    s = signals()
    assert select_current_checkpoint([checkpoint(), checkpoint(revision=2)], s) is None
    assert {w.value for w in s.warnings} == {"AMBIGUOUS_CURRENT_TRUTH", "PARTIAL_CONTEXT"}


def test_active_checkpoint_without_current_state_exposes_nothing() -> None:
    s = signals()
    assert select_current_checkpoint([checkpoint(current_state=None)], s) is None
    assert {w.value for w in s.warnings} == {"PARTIAL_CONTEXT"}


def test_unsupported_current_state_removes_continuity() -> None:
    s = signals()
    unsupported = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.EPISODE, U))
    cp = checkpoint(current_state=stmt("estado", unsupported))
    assert select_current_checkpoint([cp], s) is None
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}


def test_unsupported_statements_are_omitted_and_independent_ones_kept() -> None:
    s = signals()
    unsupported = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.EPISODE, U))
    still_ok = sup(
        SupportMode.ANY_SUFFICIENT,
        evidence(EvidenceSourceType.EPISODE, U),
        evidence(EvidenceSourceType.SESSION_ENTRY, A),
    )
    keep_pending = stmt("mantida", still_ok)
    cp = checkpoint(
        pending_work=[stmt("removida", unsupported), keep_pending],
        next_action=stmt("ação removida", unsupported),
    )
    result = select_current_checkpoint([cp], s)
    assert result is not None
    assert [item.statement_id for item in result.pending_work] == [keep_pending.statement_id]
    assert result.next_action is None
    assert result.current_state == cp.current_state
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}


# --- Episodes ---------------------------------------------------------------------------------


def test_episode_selection_keeps_source_order_and_bounds() -> None:
    s = signals()
    candidates = [EpisodeCandidate(episode(minutes=i), 0.9 - i / 10) for i in range(5)]
    selected = select_episodes(candidates, 3, s)
    assert [item.episode.episode_id for item in selected] == [
        c.episode.episode_id for c in candidates[:3]
    ]
    assert all(item.result_class == "historical_episode" for item in selected)
    assert s.warnings == set()


def test_unsupported_episode_is_excluded_and_signalled_but_null_support_is_usable() -> None:
    s = signals()
    unsupported = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.EPISODE, U))
    bad = EpisodeCandidate(episode(support=unsupported), 0.99)
    good = [EpisodeCandidate(episode(minutes=i + 1), 0.5) for i in range(3)]
    selected = select_episodes([bad, *good], 2, s)
    assert [item.episode.episode_id for item in selected] == [
        good[0].episode.episode_id,
        good[1].episode.episode_id,
    ]
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}


def test_post_forget_invariant_violation_is_excluded_not_repaired() -> None:
    s = signals()
    violating = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.MEMORY_ITEM, F))
    candidate = EpisodeCandidate(episode(support=violating), 0.9)
    assert select_episodes([candidate], 5, s) == []
    assert candidate.episode.lifecycle.value == "active"  # nothing mutated
    assert "EVIDENCE_UNAVAILABLE" in {w.value for w in s.warnings}


# --- Evidence aggregation ---------------------------------------------------------------------


def test_evidence_is_deduplicated_canonically_ordered_and_bounded() -> None:
    shared = evidence(EvidenceSourceType.SESSION_ENTRY, A)
    other = evidence(EvidenceSourceType.MEMORY_ITEM, U)
    cp_result = select_current_checkpoint(
        [checkpoint(current_state=stmt("estado", sup(SupportMode.ANY_SUFFICIENT, shared)))],
        signals(),
    )
    episodes = [
        ContextRecallEpisode(
            episode=episode(support=sup(SupportMode.ANY_SUFFICIENT, shared, other)),
            relevance=None,
        ),
        ContextRecallEpisode(
            episode=episode(support=sup(SupportMode.ANY_SUFFICIENT, shared)), relevance=None
        ),
    ]
    aggregated = aggregate_evidence(cp_result, episodes, 10)
    assert [(e.source_type, e.source_id) for e in aggregated] == [
        (other.source_type, other.source_id),
        (shared.source_type, shared.source_id),
    ]
    assert all(e.result_class == "evidence" for e in aggregated)
    assert set(aggregated[0].model_dump()) == {
        "evidence_id",
        "source_type",
        "source_id",
        "availability",
        "result_class",
    }
    assert len(aggregate_evidence(cp_result, episodes, 1)) == 1


# --- composition ------------------------------------------------------------------------------


def test_empty_healthy_project_is_a_valid_bundle_without_degradation() -> None:
    bundle = compose_bundle(request(), ContextRecallLimits(), snapshot(), semantic_degraded=False)
    assert bundle.continuity_available is False
    assert bundle.current_checkpoint is None
    assert bundle.current_memories == [] and bundle.episodes == [] and bundle.evidence == []
    assert bundle.historical_checkpoints == [] and bundle.knowledge_references == []
    assert [w.value for w in bundle.warnings] == ["NO_CURRENT_CHECKPOINT"]
    assert bundle.degraded_sources == []
    assert bundle.context_key == "main"


def test_degraded_bundle_signals_partial_context_and_sources_deterministically() -> None:
    unsupported = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.EPISODE, U))
    snap = snapshot(
        episodes=[EpisodeCandidate(episode(support=unsupported), None)],
        memories=[MemoryCandidate(memory(), None)],
    )
    bundle = compose_bundle(request(), ContextRecallLimits(), snap, semantic_degraded=True)
    assert [w.value for w in bundle.warnings] == [
        "NO_CURRENT_CHECKPOINT",
        "PARTIAL_CONTEXT",
        "EVIDENCE_UNAVAILABLE",
    ]
    assert [s.value for s in bundle.degraded_sources] == ["memories", "episodes"]
    assert bundle.current_memories[0].relevance is None  # never fabricated


def test_evidence_section_only_when_requested_but_refs_always_kept() -> None:
    ref = evidence(EvidenceSourceType.SESSION_ENTRY, A)
    snap = snapshot(
        checkpoints=[checkpoint()],
        episodes=[EpisodeCandidate(episode(support=sup(SupportMode.ALL_REQUIRED, ref)), 0.8)],
    )
    without = compose_bundle(request(), ContextRecallLimits(), snap, semantic_degraded=False)
    assert without.evidence == []
    assert without.episodes[0].episode.support is not None
    with_evidence = compose_bundle(
        request(include_evidence=True), ContextRecallLimits(), snap, semantic_degraded=False
    )
    assert [e.source_id for e in with_evidence.evidence] == [ref.source_id]
    assert with_evidence.continuity_available is True and with_evidence.warnings == []


def test_memories_are_bounded_per_source_and_keep_relevance() -> None:
    snap = snapshot(memories=[MemoryCandidate(memory(), 0.9 - i / 100) for i in range(10)])
    bundle = compose_bundle(
        request(), ContextRecallLimits(memories=3), snap, semantic_degraded=False
    )
    assert [m.relevance for m in bundle.current_memories] == [0.9, 0.89, 0.88]
    assert all(m.result_class == "current_semantic" for m in bundle.current_memories)


# --- orchestration --------------------------------------------------------------------------


class CountingEmbeddingClient:
    def __init__(self, *, fail: bool = False, dimension: int = DIM) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.dimension = dimension

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise ConnectionError("provider down")
        return [[0.1] * self.dimension for _ in texts]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened = 0

    def method(self, name: str, result: Any) -> Any:
        async def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return result

        return call


class FakeUow:
    def __init__(
        self,
        recorder: Recorder,
        *,
        fail_read: bool = False,
        read_error: BaseException | None = None,
    ) -> None:
        self.recorder = recorder
        self.fail_read = fail_read or read_error is not None
        self.read_error = read_error or OperationalError(
            "SELECT 1", {}, ConnectionRefusedError("down")
        )
        self.context_checkpoints = type(
            "C", (), {"list_active": staticmethod(recorder.method("list_active", []))}
        )()
        self.memory_items = type(
            "M",
            (),
            {
                "typed_recall": staticmethod(recorder.method("typed_recall", [])),
                "list_current_for_scope": staticmethod(
                    recorder.method("list_current_for_scope", [])
                ),
            },
        )()
        self.episodes = type(
            "E",
            (),
            {
                "contextual_semantic_search": staticmethod(
                    recorder.method("contextual_semantic_search", [])
                ),
                "list_recent_contextual": staticmethod(
                    recorder.method("list_recent_contextual", [])
                ),
            },
        )()

    async def __aenter__(self) -> FakeUow:
        self.recorder.opened += 1
        return self

    async def begin_read_only_snapshot(self) -> None:
        # The first database operation: a pooled connection is acquired here.
        self.recorder.calls.append(("begin_read_only_snapshot", {}))
        if self.fail_read:
            raise self.read_error

    async def __aexit__(self, *exc: object) -> None:
        return None


async def _no_support(uow: Any, modes: Any) -> dict[UUID, None]:
    return {owner: None for owner in modes}


def service(
    client: CountingEmbeddingClient, recorder: Recorder, *, fail_read: bool = False
) -> ContextualRecallService:
    return ContextualRecallService(
        make_settings(),
        embedding_client=client,
        unit_of_work_factory=lambda: FakeUow(recorder, fail_read=fail_read),  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_one_query_embedding_feeds_memories_and_episodes_with_project_scope_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "hydrate_episode_support", _no_support)
    client, recorder = CountingEmbeddingClient(), Recorder()
    bundle = await service(client, recorder).recall(
        request(intent="context", context_key="feature:a", limits={"memories": 5, "episodes": 4})
    )
    assert client.calls == [["Onde paramos?"]]  # exactly one provider call
    names = [name for name, _ in recorder.calls]
    assert names == [
        "begin_read_only_snapshot",  # the snapshot precedes every read
        "list_active",
        "typed_recall",
        "contextual_semantic_search",
    ]
    calls = dict(recorder.calls)
    assert calls["list_active"] == {"scope": "project:x", "context_key": "feature:a", "limit": 2}
    typed = calls["typed_recall"]
    assert typed["scopes"] == ["project:x"]  # never 'global'
    assert tuple(typed["memory_types"]) == (
        CognitiveMemoryType.PROFILE,
        CognitiveMemoryType.SEMANTIC,
    )
    assert typed["include_superseded"] is False and typed["limit"] == 5
    episodes_call = calls["contextual_semantic_search"]
    assert episodes_call["context_key"] == "feature:a" and episodes_call["limit"] == 12
    assert typed["query_embedding"] is episodes_call["query_embedding"]  # same embedding
    assert recorder.opened == 1
    assert bundle.intent.value == "context" and bundle.context_key == "feature:a"
    assert bundle.degraded_sources == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client", [CountingEmbeddingClient(fail=True), CountingEmbeddingClient(dimension=3)]
)
async def test_provider_failure_degrades_to_deterministic_fallback_without_retry(
    monkeypatch: pytest.MonkeyPatch, client: CountingEmbeddingClient
) -> None:
    monkeypatch.setattr(module, "hydrate_episode_support", _no_support)
    client.calls.clear()
    recorder = Recorder()
    bundle = await service(client, recorder).recall(request())
    assert len(client.calls) == 1  # the single failed attempt; the fallback calls nothing
    assert [name for name, _ in recorder.calls] == [
        "begin_read_only_snapshot",
        "list_active",
        "list_current_for_scope",
        "list_recent_contextual",
    ]
    fallback = dict(recorder.calls)["list_current_for_scope"]
    assert fallback["scope"] == "project:x" and fallback["limit"] == 8
    assert [w.value for w in bundle.warnings] == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert [s.value for s in bundle.degraded_sources] == ["memories", "episodes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"intent": "explain"}, "intent"),
        ({"intent": "history"}, "intent"),
        ({"datasets": ["docs"]}, "datasets"),
    ],
)
async def test_partial_contract_inputs_fail_before_provider_and_database(
    overrides: dict[str, Any], field: str
) -> None:
    client, recorder = CountingEmbeddingClient(), Recorder()
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service(client, recorder).recall(request(**overrides))
    assert excinfo.value.code.value == "INVALID_REQUEST"
    assert excinfo.value.status_code == 422
    assert excinfo.value.details["field"] == field
    assert client.calls == [] and recorder.opened == 0


@pytest.mark.asyncio
async def test_empty_datasets_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "hydrate_episode_support", _no_support)
    bundle = await service(CountingEmbeddingClient(), Recorder()).recall(request(datasets=[]))
    assert bundle.knowledge_references == []


@pytest.mark.asyncio
async def test_postgres_unavailable_fails_the_request_instead_of_fabricating() -> None:
    client, recorder = CountingEmbeddingClient(), Recorder()
    with pytest.raises(DependencyUnavailableError):
        await service(client, recorder, fail_read=True).recall(request())


def test_service_does_not_depend_on_llm_knowledge_recall_or_graph() -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    for forbidden in (
        "sofias_memory.services.recall",
        "sofias_memory.infrastructure.llm",
        "sofias_memory.infrastructure.neo4j",
        "sofias_memory.services.sessions",
        "sofias_memory.services.pipeline_submission",
    ):
        assert not {name for name in imported if name.startswith(forbidden)}, forbidden
