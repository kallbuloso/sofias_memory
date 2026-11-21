"""Unit tests for SM-CTX-003 checkpoint supersession (handoff 11): lifecycle
transitions, the full-replacement request schema, the supersede idempotency
digest, stale-writer error details, migration 0021 structure and the HTTP
route. Real-PostgreSQL behaviour lives in the opt-in integration suite."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CheckpointTransition,
    ContextCheckpointLifecycle,
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    build_contextual_canonical_request,
    checkpoint_forget_transition,
    checkpoint_invalidate_transition,
    compute_contextual_request_digest,
)
from sofias_memory.infrastructure.postgres.models import ContextCheckpoint
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointProvenanceResult,
    ContextCheckpointResult,
    ContextCheckpointSupersedeRequest,
    ContextStatementResult,
)
from sofias_memory.services.context_checkpoints import (
    canonical_create_payload,
    checkpoint_not_found_error,
    context_state_conflict_error,
)
from sofias_memory.services.pipeline_submission import idempotency_conflict_error
from tests.unit._app_factory import create_app

SECRET = b"test-cognitive-idempotency-hmac-key-0123456789abcdef"
MIGRATION_0021 = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0021_harden_context_checkpoint_supersession.py"
)
EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def body(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "scope": "project:sofias-memory",
        "context_key": "feature:contextual-memory",
        "objective": {"text": "Objetivo."},
        "current_state": {"text": "Contract aprovado e Architecture pendente."},
        "approved_decisions": [{"text": "D1"}, {"text": "D2"}],
        "pending_work": [{"text": "P1"}],
        "open_items": [],
        "blockers": [],
        "next_action": {"text": "Produzir Architecture."},
        "provenance": {"origin_kind": "assistant_generated", "source_system": "assistant"},
    }
    value.update(overrides)
    return value


def supersede_digest(target: UUID, **overrides: Any) -> str:
    request = ContextCheckpointSupersedeRequest.model_validate(body(**overrides))
    canonical = build_contextual_canonical_request(
        ContextualIdempotencyRequest(
            operation=ContextualMemoryOperation.CHECKPOINT_SUPERSEDE,
            target_resource_id=target,
            payload=canonical_create_payload(request),
        )
    )
    return compute_contextual_request_digest(secret=SECRET, canonical_request=canonical)


# --- lifecycle ------------------------------------------------------------------


def test_superseded_can_be_invalidated_and_forgotten() -> None:
    assert checkpoint_invalidate_transition(ContextCheckpointLifecycle.SUPERSEDED) is (
        CheckpointTransition.APPLY
    )
    assert checkpoint_forget_transition(ContextCheckpointLifecycle.SUPERSEDED) is (
        CheckpointTransition.APPLY
    )
    # SM-CTX-002 transitions unchanged.
    assert checkpoint_invalidate_transition(ContextCheckpointLifecycle.FORGOTTEN) is (
        CheckpointTransition.CONFLICT
    )
    assert checkpoint_forget_transition(ContextCheckpointLifecycle.FORGOTTEN) is (
        CheckpointTransition.NO_OP
    )


# --- schema ---------------------------------------------------------------------


def test_supersede_request_is_the_same_complete_snapshot_shape_as_create() -> None:
    assert set(ContextCheckpointSupersedeRequest.model_fields) == set(
        ContextCheckpointCreateRequest.model_fields
    )
    minimal = ContextCheckpointSupersedeRequest.model_validate(
        {
            "scope": "project:p",
            "current_state": {"text": "s"},
            "provenance": {"origin_kind": "user_asserted", "source_system": "s"},
        }
    )
    assert minimal.context_key == "main"
    assert minimal.objective is None and minimal.next_action is None
    assert minimal.pending_work == [] and minimal.approved_decisions == []


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_current_checkpoint_id": str(uuid4())},
        {"checkpoint_id": str(uuid4())},
        {"revision": 2},
        {"superseded_by": str(uuid4())},
        {"patch": {"next_action": None}},
    ],
)
def test_supersede_request_rejects_partial_or_server_fields(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ContextCheckpointSupersedeRequest.model_validate(body(**extra))


def test_supersede_request_requires_current_state() -> None:
    value = body()
    del value["current_state"]
    with pytest.raises(ValidationError):
        ContextCheckpointSupersedeRequest.model_validate(value)
    with pytest.raises(ValidationError):
        ContextCheckpointSupersedeRequest.model_validate(body(current_state=None))


# --- idempotency digest -----------------------------------------------------------


def test_supersede_digest_binds_target_payload_and_operation() -> None:
    target = uuid4()
    assert supersede_digest(target) == supersede_digest(target)
    assert supersede_digest(target) != supersede_digest(uuid4())
    assert supersede_digest(target) != supersede_digest(target, current_state={"text": "outro"})
    assert supersede_digest(target) != supersede_digest(
        target, pending_work=[{"text": "P1"}, {"text": "P2"}]
    )
    assert supersede_digest(target) != supersede_digest(target, next_action=None)
    create = ContextCheckpointCreateRequest.model_validate(body())
    create_digest = compute_contextual_request_digest(
        secret=SECRET,
        canonical_request=build_contextual_canonical_request(
            ContextualIdempotencyRequest(
                operation=ContextualMemoryOperation.CHECKPOINT_CREATE,
                target_resource_id=None,
                payload=canonical_create_payload(create),
            )
        ),
    )
    assert create_digest != supersede_digest(target)


# --- stale writer details ---------------------------------------------------------


def test_stale_writer_conflict_carries_expected_and_current_ids_only() -> None:
    expected, current = uuid4(), uuid4()
    error = context_state_conflict_error(current, expected_checkpoint_id=expected)
    assert error.code.value == "CONTEXT_STATE_CONFLICT" and error.status_code == 409
    assert error.details == {
        "current_checkpoint_id": str(current),
        "expected_checkpoint_id": str(expected),
    }
    none_current = context_state_conflict_error(None, expected_checkpoint_id=expected)
    assert none_current.details["current_checkpoint_id"] is None
    assert context_state_conflict_error(current).details == {"current_checkpoint_id": str(current)}


# --- migration 0021 ---------------------------------------------------------------


class OperationSpy:
    def __init__(self) -> None:
        self.created_indexes: dict[str, tuple[str, list[object], dict[str, Any]]] = {}
        self.dropped_indexes: list[str] = []
        self.executed: list[str] = []
        self.tables: list[str] = []

    def create_index(self, name: str, table: str, columns: list[object], **kw: Any) -> None:
        self.created_indexes[name] = (table, columns, kw)

    def drop_index(self, name: str, **_: Any) -> None:
        self.dropped_indexes.append(name)

    def execute(self, statement: str) -> None:
        self.executed.append(statement)

    def create_table(self, name: str, *_: object) -> None:
        self.tables.append(name)

    def drop_table(self, name: str) -> None:
        self.tables.append(name)


def run_migration(direction: str) -> tuple[Any, OperationSpy]:
    spec = importlib.util.spec_from_file_location("test_sm_ctx_003_migration", MIGRATION_0021)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    spy = OperationSpy()
    module.op = spy
    getattr(module, direction)()
    return module, spy


def test_migration_0021_chains_from_0020_and_only_hardens_lineage() -> None:
    module, spy = run_migration("upgrade")
    assert (module.revision, module.down_revision) == ("0021", "0020")
    assert spy.tables == []
    table, columns, kw = spy.created_indexes["uq_context_checkpoints_superseded_by"]
    assert table == "context_checkpoints" and columns == ["superseded_by"]
    assert kw["unique"] is True
    assert str(kw["postgresql_where"]) == "superseded_by IS NOT NULL"
    assert spy.executed == [
        "ALTER TABLE context_checkpoints ALTER CONSTRAINT "
        "fk_context_checkpoints_superseded_by_context_checkpoints "
        "DEFERRABLE INITIALLY IMMEDIATE"
    ]


def test_migration_0021_downgrade_restores_0020_shape() -> None:
    _, spy = run_migration("downgrade")
    assert spy.dropped_indexes == ["uq_context_checkpoints_superseded_by"]
    assert spy.executed == [
        "ALTER TABLE context_checkpoints ALTER CONSTRAINT "
        "fk_context_checkpoints_superseded_by_context_checkpoints NOT DEFERRABLE"
    ]


def test_orm_model_mirrors_0021() -> None:
    table = ContextCheckpoint.__table__
    index = next(i for i in table.indexes if i.name == "uq_context_checkpoints_superseded_by")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["superseded_by"]
    fk = next(iter(table.c.superseded_by.foreign_keys))
    assert fk.deferrable is True and fk.initially == "IMMEDIATE"
    assert fk.ondelete == "RESTRICT"
    assert fk.constraint is not None
    assert fk.constraint.name == "fk_context_checkpoints_superseded_by_context_checkpoints"


# --- route ------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory",
        neo4j_password="fake-neo4j-password",
        llm_api_key="sk-fake-test-key",
        cognitive_idempotency_hmac_key=SECRET.decode(),
        app_env="test",
    )


def fake_result() -> ContextCheckpointResult:
    return ContextCheckpointResult(
        checkpoint_id=uuid4(),
        scope="project:sofias-memory",
        context_key="feature:contextual-memory",
        revision=2,
        lifecycle="active",  # type: ignore[arg-type]
        objective=None,
        current_state=ContextStatementResult(statement_id=uuid4(), text="s"),
        approved_decisions=[],
        pending_work=[],
        open_items=[],
        blockers=[],
        next_action=None,
        provenance=ContextCheckpointProvenanceResult(
            origin_kind="assistant_generated",  # type: ignore[arg-type]
            source_system="assistant",
            conversation_ref=None,
            turn_ref=None,
        ),
        created_at=datetime.now(UTC),
        superseded_at=None,
        superseded_by=None,
        invalidated_at=None,
        forgotten_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect", "status", "code"),
    [
        (None, 201, None),
        (
            context_state_conflict_error(uuid4(), expected_checkpoint_id=uuid4()),
            409,
            "CONTEXT_STATE_CONFLICT",
        ),
        (idempotency_conflict_error(), 409, "IDEMPOTENCY_CONFLICT"),
        (checkpoint_not_found_error(uuid4()), 404, "CHECKPOINT_NOT_FOUND"),
    ],
)
async def test_supersede_route_status_codes(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: Exception | None,
    status: int,
    code: str | None,
) -> None:
    calls: dict[str, Any] = {}

    class FakeService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def supersede(
            self, checkpoint_id: UUID, request: Any, *, idempotency_key: str
        ) -> Any:
            calls["supersede"] = (checkpoint_id, request, idempotency_key)
            if side_effect is not None:
                raise side_effect
            return fake_result()

    monkeypatch.setattr(
        "sofias_memory.api.routes.context_checkpoints.ContextCheckpointService", FakeService
    )
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    target = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        response = await http.post(
            f"/api/v1/context/checkpoints/{target}/supersede",
            json=body(),
            headers={**HEADERS, "Idempotency-Key": "k-1"},
        )
        missing_key = await http.post(
            f"/api/v1/context/checkpoints/{target}/supersede", json=body(), headers=HEADERS
        )
        bad_key = await http.post(
            f"/api/v1/context/checkpoints/{target}/supersede",
            json=body(context_key="Main"),
            headers={**HEADERS, "Idempotency-Key": "k-2"},
        )
    assert response.status_code == status
    if code is not None:
        assert response.json()["error"]["code"] == code
    else:
        assert response.json()["data"]["revision"] == 2
    assert calls["supersede"][0] == target and calls["supersede"][2] == "k-1"
    assert missing_key.status_code == 422
    assert bad_key.status_code == 422
    assert bad_key.json()["error"]["code"] == "INVALID_CONTEXT_KEY"
