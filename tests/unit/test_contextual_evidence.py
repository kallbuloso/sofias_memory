"""Unit tests for SM-CTX-004 Evidence/support (handoff 12 SS 93-95/115): support
truth tables, per-source availability mapping, canonical sets, project
boundary, request schemas/validation codes, idempotency compatibility with the
pre-SM-CTX-004 canonical form (golden digests computed from ``main@5cedcc3``),
and migration 0022 structure."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.domain import (
    MAX_EVIDENCE_REFS_PER_SUPPORT,
    CognitiveMemoryLifecycle,
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    DatasetStatus,
    DuplicateEvidenceReferenceError,
    EpisodeLifecycle,
    EvidenceAvailability,
    EvidenceKey,
    EvidenceSourceType,
    SourceStatus,
    SupportMode,
    build_contextual_canonical_request,
    canonical_evidence_keys,
    canonical_support_payload,
    chunk_availability,
    compute_contextual_request_digest,
    document_availability,
    episode_availability,
    evaluate_support,
    evidence_project_matches,
    memory_item_availability,
    session_entry_availability,
    source_availability,
)
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.contextual_evidence import SupportRequest
from sofias_memory.schemas.episodes import EpisodeCreateRequest
from sofias_memory.services.context_checkpoints import canonical_create_payload, statement_canonical
from sofias_memory.services.episodes import _canonical_create_payload as episode_payload

A = EvidenceAvailability.AVAILABLE
U = EvidenceAvailability.UNAVAILABLE
F = EvidenceAvailability.FORGOTTEN
SECRET = b"test-cognitive-idempotency-hmac-key-0123456789abcdef"
TARGET = UUID("11111111-2222-4333-8444-555555555555")

# Computed with the pre-SM-CTX-004 code of main@5cedcc3 for the exact payloads
# below (no support): the new code must reproduce them byte for byte.
GOLDEN = {
    "episode_create": "9b3f8e455adf386df845d5543c14f464625489aad952940708b10b408549ba93",
    "checkpoint_create": "1159d035bff6832e909d984a782f3b2bc12985f003cb6246f9cd5e967ab0a17e",
    "checkpoint_supersede": "9f5342b0cd8823d1748942ab7541dba0b4bc4339d72facd16caf1270bb040044",
}
EPISODE: dict[str, Any] = {
    "scope": "project:golden",
    "context_key": "feature:golden",
    "kind": "decision",
    "summary": "Resumo dourado.",
    "situation": "Situação.",
    "reason": "Razão.",
    "outcome": "Resultado.",
    "occurred_at": "2026-11-18T20:00:00Z",
    "confidence": 0.8,
    "provenance": {
        "origin_kind": "assistant_generated",
        "source_system": "golden",
        "conversation_ref": "c-1",
        "turn_ref": "t-1",
    },
}
SNAPSHOT: dict[str, Any] = {
    "scope": "project:golden",
    "context_key": "feature:golden",
    "objective": {"text": "Objetivo."},
    "current_state": {"text": "Estado atual."},
    "approved_decisions": [{"text": "D1"}, {"text": "D2"}],
    "pending_work": [{"text": "P1"}],
    "open_items": [],
    "blockers": [{"text": "B1"}],
    "next_action": {"text": "Próxima."},
    "provenance": {"origin_kind": "user_asserted", "source_system": "golden"},
}


def digest(op: ContextualMemoryOperation, target: UUID | None, payload: dict[str, object]) -> str:
    return compute_contextual_request_digest(
        secret=SECRET,
        canonical_request=build_contextual_canonical_request(
            ContextualIdempotencyRequest(operation=op, target_resource_id=target, payload=payload)
        ),
    )


def ref(source_type: str, source_id: UUID | None = None) -> dict[str, str]:
    return {"source_type": source_type, "source_id": str(source_id or uuid4())}


# --- support evaluator truth tables (handoff 12 SS 93) -------------------------------


@pytest.mark.parametrize(
    ("availabilities", "expected"),
    [([A], True), ([A, A], True), ([A, U], False), ([A, F], False), ([U], False), ([F], False)],
)
def test_all_required(availabilities: list[EvidenceAvailability], expected: bool) -> None:
    assert evaluate_support(SupportMode.ALL_REQUIRED, availabilities) is expected


@pytest.mark.parametrize(
    ("availabilities", "expected"),
    [([A, U], True), ([F, A], True), ([U, F], False), ([F, A, U], True), ([F], False)],
)
def test_any_sufficient(availabilities: list[EvidenceAvailability], expected: bool) -> None:
    assert evaluate_support(SupportMode.ANY_SUFFICIENT, availabilities) is expected


def test_empty_support_is_never_evaluated_as_valid() -> None:
    for mode in SupportMode:
        with pytest.raises(ValueError):
            evaluate_support(mode, [])


# --- availability mapping (handoff 12 SS 38-45) ---------------------------------------


def test_session_entry_memory_item_and_episode_mapping() -> None:
    assert session_entry_availability(exists=True) is A
    assert session_entry_availability(exists=False) is U
    assert memory_item_availability(CognitiveMemoryLifecycle.ACTIVE) is A
    assert memory_item_availability(CognitiveMemoryLifecycle.SUPERSEDED) is A
    assert memory_item_availability(CognitiveMemoryLifecycle.FORGOTTEN) is F
    assert memory_item_availability(None) is U
    assert episode_availability(EpisodeLifecycle.ACTIVE) is A
    assert episode_availability(EpisodeLifecycle.INVALIDATED) is U
    assert episode_availability(EpisodeLifecycle.FORGOTTEN) is F
    assert episode_availability(None) is U


@pytest.mark.parametrize(
    ("source", "dataset", "expected"),
    [
        (SourceStatus.ACTIVE, DatasetStatus.ACTIVE, A),
        (SourceStatus.PENDING, DatasetStatus.ACTIVE, U),
        (SourceStatus.PROCESSING, DatasetStatus.ACTIVE, U),
        (SourceStatus.FAILED, DatasetStatus.ACTIVE, U),
        (SourceStatus.DELETING, DatasetStatus.ACTIVE, U),
        (SourceStatus.DELETED, DatasetStatus.ACTIVE, F),
        (SourceStatus.ACTIVE, DatasetStatus.DELETING, U),
        (SourceStatus.ACTIVE, DatasetStatus.DELETED, F),
        (None, None, U),
    ],
)
def test_source_mapping(
    source: SourceStatus | None, dataset: DatasetStatus | None, expected: EvidenceAvailability
) -> None:
    assert source_availability(source_status=source, dataset_status=dataset) is expected


def doc(**overrides: Any) -> EvidenceAvailability:
    values: dict[str, Any] = {
        "document_is_active": True,
        "document_generation": 3,
        "source_status": SourceStatus.ACTIVE,
        "dataset_status": DatasetStatus.ACTIVE,
        "dataset_active_generation": 3,
    }
    values.update(overrides)
    return document_availability(**values)


def chunk(**overrides: Any) -> EvidenceAvailability:
    values: dict[str, Any] = {
        "chunk_is_active": True,
        "chunk_generation": 3,
        "document_is_active": True,
        "document_generation": 3,
        "source_status": SourceStatus.ACTIVE,
        "dataset_status": DatasetStatus.ACTIVE,
        "dataset_active_generation": 3,
    }
    values.update(overrides)
    return chunk_availability(**values)


def test_document_and_chunk_follow_authoritative_generation_not_existence() -> None:
    assert doc() is A
    assert doc(document_is_active=False) is U
    assert doc(document_generation=2) is U
    assert doc(source_status=SourceStatus.DELETED) is F
    assert doc(document_is_active=None) is U
    assert chunk() is A
    assert chunk(chunk_is_active=False) is U
    assert chunk(chunk_generation=2) is U
    assert chunk(document_is_active=False) is U
    assert chunk(source_status=SourceStatus.DELETED) is F
    assert chunk(dataset_status=DatasetStatus.DELETED) is F
    assert chunk(chunk_is_active=None) is U


# --- canonical set / project boundary --------------------------------------------------


def test_evidence_is_an_unordered_set_and_duplicates_are_rejected() -> None:
    a = EvidenceKey(EvidenceSourceType.EPISODE, uuid4())
    b = EvidenceKey(EvidenceSourceType.CHUNK, uuid4())
    assert canonical_evidence_keys([a, b]) == canonical_evidence_keys([b, a])
    assert canonical_support_payload(SupportMode.ALL_REQUIRED, [a, b]) == canonical_support_payload(
        SupportMode.ALL_REQUIRED, [b, a]
    )
    with pytest.raises(DuplicateEvidenceReferenceError):
        canonical_evidence_keys([a, a])
    same_uuid = uuid4()
    assert (
        len(
            canonical_evidence_keys(
                [
                    EvidenceKey(EvidenceSourceType.EPISODE, same_uuid),
                    EvidenceKey(EvidenceSourceType.MEMORY_ITEM, same_uuid),
                ]
            )
        )
        == 2
    )


def test_project_boundary() -> None:
    owner = "project:a"
    E, M, S = EvidenceSourceType.EPISODE, EvidenceSourceType.MEMORY_ITEM, EvidenceSourceType.SOURCE
    assert evidence_project_matches(owner_scope=owner, source_type=E, source_scope="project:a")
    assert not evidence_project_matches(owner_scope=owner, source_type=E, source_scope="project:b")
    assert evidence_project_matches(owner_scope=owner, source_type=M, source_scope="project:a")
    assert evidence_project_matches(owner_scope=owner, source_type=M, source_scope="global")
    assert not evidence_project_matches(owner_scope=owner, source_type=M, source_scope="project:b")
    assert evidence_project_matches(owner_scope=owner, source_type=S, source_scope="project:b")
    assert evidence_project_matches(owner_scope=owner, source_type=E, source_scope=None)


# --- request schema ----------------------------------------------------------------------


def error_types(error: ValidationError) -> set[str]:
    return {item["type"] for item in error.errors()}


def test_support_request_validation_codes() -> None:
    ok = SupportRequest.model_validate({"mode": "all_required", "refs": [ref("episode")]})
    assert ok.mode is SupportMode.ALL_REQUIRED
    cases = [
        ({"mode": "all_required", "refs": []}, "invalid_support_policy"),
        ({"mode": "majority", "refs": [ref("episode")]}, "invalid_support_policy"),
        (
            {
                "mode": "all_required",
                "refs": [ref("episode") for _ in range(MAX_EVIDENCE_REFS_PER_SUPPORT + 1)],
            },
            "invalid_support_policy",
        ),
        ({"mode": "all_required", "refs": [ref("dataset")]}, "invalid_evidence_reference"),
        (
            {"mode": "all_required", "refs": [ref("context_statement")]},
            "invalid_evidence_reference",
        ),
        ({"mode": "all_required", "evidence": [ref("episode")]}, "extra_forbidden"),
    ]
    for body, expected in cases:
        with pytest.raises(ValidationError) as caught:
            SupportRequest.model_validate(body)
        assert expected in error_types(caught.value), body
    dup = ref("episode")
    with pytest.raises(ValidationError) as caught:
        SupportRequest.model_validate({"mode": "all_required", "refs": [dup, dup]})
    assert "value_error" in error_types(caught.value)
    assert SupportRequest.model_validate(
        {
            "mode": "any_sufficient",
            "refs": [ref("episode") for _ in range(MAX_EVIDENCE_REFS_PER_SUPPORT)],
        }
    )


def test_support_is_optional_everywhere() -> None:
    assert EpisodeCreateRequest.model_validate(EPISODE).support is None
    snapshot = ContextCheckpointCreateRequest.model_validate(SNAPSHOT)
    assert snapshot.current_state.support is None


# --- idempotency compatibility (handoff 12 SS 71-73) ----------------------------------------


def test_payloads_without_support_reproduce_pre_sm_ctx_004_digests() -> None:
    episode = EpisodeCreateRequest.model_validate(EPISODE)
    assert "support" not in episode_payload(episode)
    assert (
        digest(ContextualMemoryOperation.EPISODE_CREATE, None, episode_payload(episode))
        == GOLDEN["episode_create"]
    )
    create = ContextCheckpointCreateRequest.model_validate(SNAPSHOT)
    assert (
        digest(ContextualMemoryOperation.CHECKPOINT_CREATE, None, canonical_create_payload(create))
        == GOLDEN["checkpoint_create"]
    )
    supersede = ContextCheckpointSupersedeRequest.model_validate(SNAPSHOT)
    assert (
        digest(
            ContextualMemoryOperation.CHECKPOINT_SUPERSEDE,
            TARGET,
            canonical_create_payload(supersede),
        )
        == GOLDEN["checkpoint_supersede"]
    )


def test_declared_support_changes_digest_but_order_does_not() -> None:
    a, b = ref("episode"), ref("session_entry")
    forward = EpisodeCreateRequest.model_validate(
        {**EPISODE, "support": {"mode": "all_required", "refs": [a, b]}}
    )
    backward = EpisodeCreateRequest.model_validate(
        {**EPISODE, "support": {"mode": "all_required", "refs": [b, a]}}
    )
    other_mode = EpisodeCreateRequest.model_validate(
        {**EPISODE, "support": {"mode": "any_sufficient", "refs": [a, b]}}
    )
    op = ContextualMemoryOperation.EPISODE_CREATE
    assert digest(op, None, episode_payload(forward)) == digest(op, None, episode_payload(backward))
    assert digest(op, None, episode_payload(forward)) != GOLDEN["episode_create"]
    assert digest(op, None, episode_payload(forward)) != digest(
        op, None, episode_payload(other_mode)
    )

    statement_forward = {
        **SNAPSHOT,
        "current_state": {
            "text": "Estado atual.",
            "support": {"mode": "all_required", "refs": [a, b]},
        },
    }
    statement_backward = {
        **SNAPSHOT,
        "current_state": {
            "text": "Estado atual.",
            "support": {"mode": "all_required", "refs": [b, a]},
        },
    }
    cop = ContextualMemoryOperation.CHECKPOINT_CREATE
    f = canonical_create_payload(ContextCheckpointCreateRequest.model_validate(statement_forward))
    g = canonical_create_payload(ContextCheckpointCreateRequest.model_validate(statement_backward))
    assert digest(cop, None, f) == digest(cop, None, g) != GOLDEN["checkpoint_create"]


def test_statement_canonical_is_bare_text_without_support() -> None:
    snapshot = ContextCheckpointCreateRequest.model_validate(SNAPSHOT)
    assert statement_canonical(snapshot.current_state) == "Estado atual."


# --- migration 0022 ------------------------------------------------------------------------------

MIGRATION_0022 = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0022_create_contextual_evidence.py"
)


class Spy:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[object, ...]] = {}
        self.indexes: list[str] = []
        self.columns: list[tuple[str, str, bool]] = []
        self.checks: list[tuple[str, str, str]] = []
        self.dropped: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.tables[name] = objects

    def create_index(self, name: str, *_: Any, **__: Any) -> None:
        self.indexes.append(name)

    def add_column(self, table: str, column: Any) -> None:
        self.columns.append((table, column.name, column.nullable))

    def create_check_constraint(self, name: str, table: str, condition: str) -> None:
        self.checks.append((name, table, condition))

    def drop_column(self, table: str, column: str) -> None:
        self.dropped.append(f"col:{table}.{column}")

    def drop_constraint(self, name: str, table: str, **_: Any) -> None:
        self.dropped.append(f"ck:{name}")

    def drop_index(self, name: str, **_: Any) -> None:
        self.dropped.append(f"ix:{name}")

    def drop_table(self, name: str) -> None:
        self.dropped.append(f"t:{name}")


def run_0022(direction: str) -> tuple[Any, Spy, list[str]]:
    spec = importlib.util.spec_from_file_location("test_sm_ctx_004_migration", MIGRATION_0022)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    enums: list[str] = []
    for name in ("evidence_source_type", "support_mode"):
        getattr(module, name).create = lambda bind, _n=name: enums.append(f"+{_n}")
        getattr(module, name).drop = lambda bind, _n=name: enums.append(f"-{_n}")
    spy = Spy()
    module.op = spy
    getattr(module, direction)()
    return module, spy, enums


def test_migration_0022_structure() -> None:
    from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

    module, spy, enums = run_0022("upgrade")
    assert (module.revision, module.down_revision) == ("0022", "0021")
    assert enums == ["+evidence_source_type", "+support_mode"]
    assert list(module.evidence_source_type.enums) == [
        "session_entry",
        "memory_item",
        "episode",
        "source",
        "document",
        "chunk",
    ]
    assert list(module.support_mode.enums) == ["all_required", "any_sufficient"]
    assert set(spy.tables) == {
        "contextual_evidence_refs",
        "episode_evidence_refs",
        "context_statement_evidence_refs",
    }
    uniques = [
        o.name for o in spy.tables["contextual_evidence_refs"] if isinstance(o, UniqueConstraint)
    ]
    assert uniques == ["uq_contextual_evidence_refs_source"]
    assert "availability" not in {
        getattr(o, "name", None) for o in spy.tables["contextual_evidence_refs"]
    }
    fks = {
        o.name: o.ondelete
        for t in ("episode_evidence_refs", "context_statement_evidence_refs")
        for o in spy.tables[t]
        if isinstance(o, ForeignKeyConstraint)
    }
    assert fks == {
        "fk_episode_evidence_refs_episode_id": "CASCADE",
        "fk_episode_evidence_refs_evidence_ref_id": "RESTRICT",
        "fk_context_statement_evidence_refs_statement_id": "CASCADE",
        "fk_context_statement_evidence_refs_evidence_ref_id": "RESTRICT",
    }
    assert sorted(spy.columns) == [
        ("context_checkpoint_statements", "support_mode", True),
        ("episodes", "support_mode", True),
    ]
    assert spy.checks == [
        (
            "ck_episodes_forgotten_has_no_support",
            "episodes",
            "lifecycle <> 'forgotten' OR support_mode IS NULL",
        )
    ]
    names = [
        *spy.indexes,
        *[o.name for t in spy.tables.values() for o in t if getattr(o, "name", None)],
    ]
    assert all(len(str(n).encode()) <= 63 for n in names)


def test_migration_0022_downgrade_reverses_everything() -> None:
    _, spy, enums = run_0022("downgrade")
    assert enums == ["-support_mode", "-evidence_source_type"]
    assert "t:contextual_evidence_refs" in spy.dropped
    assert "t:episode_evidence_refs" in spy.dropped
    assert "t:context_statement_evidence_refs" in spy.dropped
    assert "col:episodes.support_mode" in spy.dropped
    assert "col:context_checkpoint_statements.support_mode" in spy.dropped
    assert "ck:ck_episodes_forgotten_has_no_support" in spy.dropped
