"""PR #3 review corrections for SM-CTX-006 Contextual Recall (unit proof):
declared support with missing links fails closed, the internal bundle budget,
pool-acquisition timeouts and invalid query embeddings. Snapshot consistency
under a concurrent Forget is proven against real PostgreSQL in
``tests/integration/test_contextual_recall_postgres_integration.py``."""

from __future__ import annotations

import math
from typing import Any

import pytest
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from structlog.testing import capture_logs

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.domain import EvidenceSourceType, SupportMode
from sofias_memory.schemas.context_recall import ContextRecallLimits, ProjectContextBundle
from sofias_memory.services import contextual_recall as module
from sofias_memory.services.contextual_recall import (
    CONTEXT_RECALL_MAX_BUNDLE_BYTES,
    CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS,
    EpisodeCandidate,
    MemoryCandidate,
    SourceSnapshot,
    checkpoint_statement_count,
    compose_bundle,
    is_valid_query_embedding,
    select_current_checkpoint,
    select_episodes,
    serialized_bundle_bytes,
)
from tests.unit.test_contextual_recall_service import (
    DIM,
    A,
    CountingEmbeddingClient,
    FakeUow,
    Recorder,
    _no_support,
    checkpoint,
    episode,
    evidence,
    make_settings,
    memory,
    request,
    signals,
    stmt,
    sup,
)

# --- FIX 2: declared support whose links are missing ------------------------------------------


def _violations(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in logs
        if entry.get("event") == "context_recall_invariant_violation"
        and entry.get("reason") == "declared_support_without_links"
    ]


def test_episode_with_declared_support_but_no_links_is_excluded_fail_closed() -> None:
    s = signals()
    malformed = EpisodeCandidate(episode(), 0.99, support_declared=True)  # support=None
    independent = EpisodeCandidate(episode(minutes=1), 0.5)  # support_mode=None
    with capture_logs() as logs:
        selected = select_episodes([malformed, independent], 5, s)
    assert [item.episode.episode_id for item in selected] == [independent.episode.episode_id]
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}
    violations = _violations(logs)
    assert [v["owner_id"] for v in violations] == [str(malformed.episode.episode_id)]
    assert violations[0]["owner_kind"] == "episode"
    assert set(violations[0]) <= {"event", "log_level", "owner_kind", "owner_id", "reason"}
    assert malformed.episode.lifecycle.value == "active"  # never repaired


def test_statement_with_declared_support_but_no_links_is_omitted() -> None:
    s = signals()
    malformed = stmt("pendência sem links")  # support=None, but persisted mode is set
    independent = stmt("pendência independente")  # support_mode=None
    cp = checkpoint(pending_work=[malformed, independent])
    with capture_logs() as logs:
        result = select_current_checkpoint([cp], s, frozenset({malformed.statement_id}))
    assert result is not None
    assert [item.statement_id for item in result.pending_work] == [independent.statement_id]
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}
    assert [v["owner_id"] for v in _violations(logs)] == [str(malformed.statement_id)]
    assert "pendência sem links" not in result.model_dump_json()


def test_current_state_with_declared_support_but_no_links_removes_continuity() -> None:
    s = signals()
    cp = checkpoint()
    assert cp.current_state is not None
    result = select_current_checkpoint([cp], s, frozenset({cp.current_state.statement_id}))
    assert result is None
    assert {w.value for w in s.warnings} == {"EVIDENCE_UNAVAILABLE", "PARTIAL_CONTEXT"}


def test_null_support_mode_remains_an_independent_assertion() -> None:
    s = signals()
    cp = checkpoint()
    result = select_current_checkpoint([cp], s, frozenset())
    assert result is not None and s.warnings == set()
    assert select_episodes([EpisodeCandidate(episode(), 0.9)], 5, s) != []
    assert s.warnings == set()


def test_compose_uses_the_snapshot_declared_statement_ids() -> None:
    cp = checkpoint()
    assert cp.objective is not None
    snap = SourceSnapshot(
        active_checkpoints=[cp],
        memories=[],
        episodes=[],
        declared_statement_ids=frozenset({cp.objective.statement_id}),
    )
    bundle = compose_bundle(request(), request().limits.effective(), snap, semantic_degraded=False)
    assert bundle.current_checkpoint is not None
    assert bundle.current_checkpoint.objective is None
    assert [w.value for w in bundle.warnings] == ["PARTIAL_CONTEXT", "EVIDENCE_UNAVAILABLE"]


# --- FIX 3: bounded ProjectContextBundle -------------------------------------------------------


def limits(**overrides: int) -> ContextRecallLimits:
    return ContextRecallLimits.model_validate(overrides).effective()


def mem(content: str, relevance: float | None = 0.5) -> MemoryCandidate:
    return MemoryCandidate(memory().model_copy(update={"content": content}), relevance)


def ep(summary: str, minutes: int = 0, support: Any = None) -> EpisodeCandidate:
    return EpisodeCandidate(
        episode(minutes=minutes, support=support).model_copy(update={"summary": summary}), 0.5
    )


def snap(
    cps: list[Any] | None = None,
    memories: list[MemoryCandidate] | None = None,
    episodes: list[EpisodeCandidate] | None = None,
) -> SourceSnapshot:
    return SourceSnapshot(
        active_checkpoints=cps or [], memories=memories or [], episodes=episodes or []
    )


def compose(snapshot: SourceSnapshot, **kwargs: Any) -> ProjectContextBundle:
    req = kwargs.pop("req", request(include_evidence=True))
    lim = kwargs.pop("lim", limits(memories=20, episodes=20, evidence=50))
    return compose_bundle(
        req, lim, snapshot, semantic_degraded=kwargs.pop("degraded", False), **kwargs
    )


def warnings(bundle: ProjectContextBundle) -> list[str]:
    return [w.value for w in bundle.warnings]


def test_approved_initial_caps() -> None:
    assert CONTEXT_RECALL_MAX_BUNDLE_BYTES == 262_144  # 256 KiB UTF-8
    assert CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS == 64


def test_bundle_within_budget_is_untouched_and_normal_absence_is_not_degradation() -> None:
    empty = compose(snap())
    assert warnings(empty) == ["NO_CURRENT_CHECKPOINT"]  # absence, not budget degradation
    assert empty.degraded_sources == []
    full = compose(snap([checkpoint()], [mem("fato")], [ep("resumo")]))
    assert warnings(full) == [] and full.continuity_available is True


def test_oversized_checkpoint_is_omitted_whole_and_its_rows_untouched() -> None:
    big = checkpoint(current_state=stmt("x" * (CONTEXT_RECALL_MAX_BUNDLE_BYTES + 1)))
    kept_memory = mem("fato pequeno")
    with capture_logs() as logs:
        bundle = compose(snap([big], [kept_memory], [ep("resumo")]))
    assert bundle.current_checkpoint is None and bundle.continuity_available is False
    assert warnings(bundle) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert [m.memory.memory_id for m in bundle.current_memories] == [kept_memory.memory.memory_id]
    assert len(bundle.episodes) == 1  # other resources still included while they fit
    assert serialized_bundle_bytes(bundle) <= CONTEXT_RECALL_MAX_BUNDLE_BYTES
    assert big.current_state is not None and len(big.current_state.text) > 262_144  # unchanged
    omission = [e for e in logs if e["event"] == "context_recall_budget_omission"]
    assert omission and omission[0]["checkpoint_omitted"] is True
    assert "x" * 100 not in str(omission)  # identifiers/counters only, never content


def _checkpoint_with(total: int) -> Any:
    # objective + current_state + next_action = 3, the rest are pending_work.
    return checkpoint(approved_decisions=[], pending_work=[stmt(f"p{i}") for i in range(total - 3)])


def test_checkpoint_statement_cap_boundary() -> None:
    at_cap = _checkpoint_with(CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS)
    result = compose(snap([at_cap]))
    assert result.current_checkpoint is not None
    assert checkpoint_statement_count(result.current_checkpoint) == 64
    assert warnings(result) == []

    over = compose(snap([_checkpoint_with(CONTEXT_RECALL_MAX_CHECKPOINT_STATEMENTS + 1)]))
    assert over.current_checkpoint is None and over.continuity_available is False
    assert warnings(over) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]


def test_statement_cap_counts_the_eligible_statements_only() -> None:
    unsupported = sup(SupportMode.ALL_REQUIRED, evidence(EvidenceSourceType.EPISODE, A))
    unsupported.evidence[0].availability = module.EvidenceAvailability.UNAVAILABLE
    cp = checkpoint(
        approved_decisions=[],
        pending_work=[stmt(f"p{i}") for i in range(61)] + [stmt("removida", unsupported)],
    )
    bundle = compose(snap([cp]))  # 65 persisted, 64 eligible -> fits
    assert bundle.current_checkpoint is not None
    assert checkpoint_statement_count(bundle.current_checkpoint) == 64


def test_large_individual_memory_is_omitted_never_truncated() -> None:
    first = mem("primeiro")
    huge = mem("y" * CONTEXT_RECALL_MAX_BUNDLE_BYTES)
    after = mem("depois")
    bundle = compose(snap([checkpoint()], [first, huge, after], [ep("resumo")]))
    contents = [m.memory.content for m in bundle.current_memories]
    assert contents == ["primeiro"]  # a ranked prefix; nothing past the gap
    assert all(len(c) < 100 for c in contents)  # no truncated huge text
    assert bundle.current_checkpoint is not None and len(bundle.episodes) == 1
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]


def test_evidence_fan_out_counts_against_the_budget() -> None:
    refs = [evidence(EvidenceSourceType.SESSION_ENTRY, A) for _ in range(50)]
    fan_out = sup(SupportMode.ANY_SUFFICIENT, *refs)
    episodes = [ep(f"e{i}", minutes=i, support=fan_out) for i in range(3)]
    unbounded = compose(snap(episodes=episodes))
    one = serialized_bundle_bytes(compose(snap(episodes=episodes[:1])))
    two = serialized_bundle_bytes(compose(snap(episodes=episodes[:2])))
    assert two - one > 50 * 100  # every Episode carries its 50 nested references
    bounded = compose(snap(episodes=episodes), max_bundle_bytes=two + 40)
    assert len(unbounded.episodes) == 3 and len(bounded.episodes) == 2
    assert len(bounded.evidence) == 50  # aggregated once, still bounded by limits.evidence
    assert serialized_bundle_bytes(bounded) <= two + 40
    assert "PARTIAL_CONTEXT" in warnings(bounded)


def test_combined_budget_keeps_priority_checkpoint_memories_then_episodes() -> None:
    cp = checkpoint(current_state=stmt("c" * 100_000))
    memories = [mem("m" * 60_000), mem("n" * 60_000)]
    episodes = [ep("e" * 60_000), ep("f" * 10, minutes=1)]
    bundle = compose(snap([cp], memories, episodes))
    assert bundle.current_checkpoint is not None
    assert [m.memory.content[0] for m in bundle.current_memories] == ["m", "n"]
    assert [e.episode.summary for e in bundle.episodes] == []  # the first Episode does not fit
    assert warnings(bundle) == ["PARTIAL_CONTEXT"]
    assert serialized_bundle_bytes(bundle) <= CONTEXT_RECALL_MAX_BUNDLE_BYTES


def test_budgeted_composition_is_deterministic() -> None:
    snapshot = snap(
        [checkpoint(current_state=stmt("c" * 100_000))],
        [mem("m" * 70_000), mem("n" * 70_000)],
        [ep("e" * 1_000, minutes=i) for i in range(5)],
    )
    first = compose(snapshot).model_dump_json()
    assert all(compose(snapshot).model_dump_json() == first for _ in range(5))


def test_budget_counts_utf8_bytes_not_characters() -> None:
    text = "ç" * 100_000  # 100k characters, 200k bytes
    bundle_two = compose(snap(memories=[mem(text), mem(text)]))
    assert [len(m.memory.content) for m in bundle_two.current_memories] == [100_000]
    assert warnings(bundle_two) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert serialized_bundle_bytes(bundle_two) <= CONTEXT_RECALL_MAX_BUNDLE_BYTES
    assert len(bundle_two.model_dump_json()) < serialized_bundle_bytes(bundle_two)


def test_exact_serialized_size_boundary() -> None:
    # Degraded: PARTIAL_CONTEXT is present either way, so the final size is exact.
    snapshot = snap([checkpoint()], [mem("fato")], [ep("resumo"), ep("último 😀", minutes=1)])
    complete = compose(snapshot, degraded=True)
    size = serialized_bundle_bytes(complete)

    at_boundary = compose(snapshot, degraded=True, max_bundle_bytes=size)
    assert at_boundary.model_dump_json() == complete.model_dump_json()
    assert serialized_bundle_bytes(at_boundary) == size

    below = compose(snapshot, degraded=True, max_bundle_bytes=size - 1)
    assert [e.episode.summary for e in below.episodes] == ["resumo"]  # last one omitted whole
    assert below.current_checkpoint is not None and len(below.current_memories) == 1
    assert serialized_bundle_bytes(below) <= size - 1


def test_bundle_size_matches_the_http_serialization() -> None:
    import json

    bundle = compose(snap([checkpoint()], [mem("ação 😀")], [ep("resumo")]))
    as_http = json.dumps(
        bundle.model_dump(mode="json", by_alias=True), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(as_http) == serialized_bundle_bytes(bundle)


def test_a_bound_below_the_empty_bundle_fails_instead_of_overflowing() -> None:
    with pytest.raises(SofiasMemoryError) as excinfo:
        compose(snap(memories=[mem("fato")]), max_bundle_bytes=10)
    assert excinfo.value.status_code == 500


# --- FIX 4: pool-acquisition timeout -----------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_acquisition_timeout_is_dependency_unavailable() -> None:
    client, recorder = CountingEmbeddingClient(), Recorder()
    timeout = PoolTimeoutError("QueuePool limit of size 1 overflow 0 reached, timed out")
    svc = module.ContextualRecallService(
        make_settings(),
        embedding_client=client,
        unit_of_work_factory=lambda: FakeUow(recorder, read_error=timeout),  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(DependencyUnavailableError) as excinfo:
        await svc.recall(request())
    assert excinfo.value.status_code == 503
    assert excinfo.value.code.value == "DEPENDENCY_UNAVAILABLE"
    assert excinfo.value.__cause__ is timeout
    assert len(client.calls) == 1


def test_only_the_sqlalchemy_pool_timeout_is_added_to_the_mapping() -> None:
    # sqlalchemy.exc.TimeoutError is not a builtin TimeoutError/OSError, so the
    # pre-existing (OperationalError, InterfaceError, OSError) mapping missed it.
    assert not issubclass(PoolTimeoutError, OSError)


@pytest.mark.asyncio
async def test_unrelated_application_errors_are_not_mapped_to_dependency_unavailable() -> None:
    svc = module.ContextualRecallService(
        make_settings(),
        embedding_client=CountingEmbeddingClient(),
        unit_of_work_factory=lambda: FakeUow(Recorder(), read_error=ValueError("app bug")),  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(ValueError, match="app bug"):
        await svc.recall(request())


# --- FIX 5: invalid query embeddings -----------------------------------------------------------


def _vector(**at: float) -> list[float]:
    vector = [0.0] * DIM
    for index, value in at.items():
        vector[int(index.removeprefix("i"))] = value
    return vector


INVALID_VECTORS: dict[str, list[Any]] = {
    "zero": [0.0] * DIM,
    "nan": _vector(i0=1.0, i5=math.nan),
    "positive_infinity": _vector(i0=1.0, i7=math.inf),
    "negative_infinity": _vector(i0=1.0, i9=-math.inf),
    "float4_overflow": _vector(i0=1e39),
    "float4_underflow_to_zero": [1e-50] * DIM,
    "non_numeric": [*([0.1] * (DIM - 1)), "x"],
    "too_short": [0.1] * (DIM - 1),
    "too_long": [0.1] * (DIM + 1),
}


class FixedEmbeddingClient(CountingEmbeddingClient):
    def __init__(self, vector: list[Any]) -> None:
        super().__init__()
        self.vector = vector

    async def embed_texts(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector]


@pytest.mark.parametrize("kind", sorted(INVALID_VECTORS))
def test_invalid_vector_classes_are_rejected(kind: str) -> None:
    vector = INVALID_VECTORS[kind]
    if len(vector) == DIM:
        assert is_valid_query_embedding(vector) is False


def test_valid_vectors_are_accepted() -> None:
    assert is_valid_query_embedding([0.1] * DIM)
    assert is_valid_query_embedding(_vector(i3=-0.5))
    assert is_valid_query_embedding(_vector(i0=1e-30))  # tiny but nonzero in float4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(INVALID_VECTORS))
async def test_invalid_embedding_degrades_like_an_unavailable_provider_without_retry(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(module, "hydrate_episode_support", _no_support)
    client, recorder = FixedEmbeddingClient(INVALID_VECTORS[kind]), Recorder()
    svc = module.ContextualRecallService(
        make_settings(),
        embedding_client=client,
        unit_of_work_factory=lambda: FakeUow(recorder),  # type: ignore[arg-type,return-value]
    )
    with capture_logs() as logs:
        bundle = await svc.recall(request())
    assert len(client.calls) == 1  # single attempt, never retried
    assert [name for name, _ in recorder.calls] == [
        "begin_read_only_snapshot",
        "list_active",
        "list_current_for_scope",  # deterministic fallback, no vector reaches SQL
        "list_recent_contextual",
    ]
    assert warnings(bundle) == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert [s.value for s in bundle.degraded_sources] == ["memories", "episodes"]
    assert [e["event"] for e in logs if e["event"] == "context_recall_embedding_unavailable"]
