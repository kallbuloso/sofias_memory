from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun
from sofias_memory.services.pipeline_queue_claimer import (
    ClaimedRun,
    PipelineRunClaimer,
    new_worker_id,
)

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def make_run(
    *,
    dataset_id: UUID | None,
    run_id: UUID | None = None,
    worker_id: str | None = None,
    attempt: int = 0,
) -> PipelineRun:
    return PipelineRun(
        id=run_id or uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        status=PipelineRunStatus.QUEUED,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={},
        progress=0.0,
        current_step=None,
        attempt=attempt,
        worker_id=worker_id,
        heartbeat_at=None,
        config_fingerprint="b" * 64,
        error_code=None,
        error_message=None,
        metrics={},
        created_at=NOW,
        started_at=None,
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=None,
    )


# --- new_worker_id / ClaimedRun (pure) --------------------------------------


def test_new_worker_id_has_stable_opaque_prefix() -> None:
    worker_id = new_worker_id()
    assert worker_id.startswith("wk-")


def test_new_worker_id_is_unique_per_call() -> None:
    assert new_worker_id() != new_worker_id()


def test_new_worker_id_never_embeds_hostname_or_pid() -> None:
    import os
    import socket

    worker_id = new_worker_id()
    assert str(os.getpid()) not in worker_id
    assert socket.gethostname() not in worker_id


def test_claimed_run_from_model_happy_path() -> None:
    dataset_id = uuid4()
    run = make_run(dataset_id=dataset_id, worker_id="wk-abc", attempt=1)
    claimed = ClaimedRun.from_model(run)
    assert claimed.run_id == run.id
    assert claimed.dataset_id == dataset_id
    assert claimed.pipeline_type == PipelineType.REMEMBER
    assert claimed.worker_id == "wk-abc"
    assert claimed.attempt == 1


def test_claimed_run_from_model_requires_worker_id() -> None:
    run = make_run(dataset_id=None, worker_id=None)
    with pytest.raises(AssertionError):
        ClaimedRun.from_model(run)


# --- arbitration branching (pure control flow, fake repository) ------------


@dataclass
class FakeClaimRepository:
    """Records calls and returns configured answers -- proves the claimer's
    branching/short-circuit order, not PostgreSQL concurrency (that is the
    opt-in integration suite's job)."""

    eligible_global_precedence: bool = False
    shared_lock_ok: bool = True
    exclusive_dataset_lock_ok: bool = True
    exclusive_global_lock_ok: bool = True
    dataset_conflict: bool = False
    any_dataset_scoped_conflict: bool = False
    other_global_conflict: bool = False
    calls: list[str] = field(default_factory=list)

    async def exists_eligible_global_with_precedence(
        self, *, before_created_at: datetime, before_id: UUID
    ) -> bool:
        del before_created_at, before_id
        self.calls.append("fairness")
        return self.eligible_global_precedence

    async def try_advisory_lock_shared(self, key: int) -> bool:
        del key
        self.calls.append("shared_lock")
        return self.shared_lock_ok

    async def try_advisory_lock_exclusive(self, key: int) -> bool:
        del key
        self.calls.append("exclusive_lock")
        if "shared_lock" in self.calls or "fairness" in self.calls:
            # Distinguish dataset-scoped exclusive from global exclusive by
            # call context isn't possible generically here; tests configure
            # a single relevant flag per scenario instead.
            pass
        return self.exclusive_dataset_lock_ok and self.exclusive_global_lock_ok

    async def exists_dataset_conflict(self, dataset_id: UUID) -> bool:
        del dataset_id
        self.calls.append("dataset_conflict")
        return self.dataset_conflict

    async def exists_any_dataset_scoped_conflict(self) -> bool:
        self.calls.append("any_dataset_scoped_conflict")
        return self.any_dataset_scoped_conflict

    async def exists_other_global_conflict(self, *, exclude_run_id: UUID) -> bool:
        del exclude_run_id
        self.calls.append("other_global_conflict")
        return self.other_global_conflict


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_succeeds_when_all_clear() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository()
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is True
    assert repo.calls == [
        "fairness",
        "shared_lock",
        "exclusive_lock",
        "dataset_conflict",
        "other_global_conflict",
    ]


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_blocked_by_fairness_before_any_lock() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository(eligible_global_precedence=True)
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls == ["fairness"]  # never attempted a lock at all


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_aborts_when_shared_lock_unavailable() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository(shared_lock_ok=False)
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls == ["fairness", "shared_lock"]  # never revalidates


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_aborts_when_exclusive_lock_unavailable() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository(exclusive_dataset_lock_ok=False)
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls == ["fairness", "shared_lock", "exclusive_lock"]


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_blocked_by_dataset_conflict() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository(dataset_conflict=True)
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls[-1] == "dataset_conflict"
    assert "other_global_conflict" not in repo.calls  # short-circuits


@pytest.mark.asyncio
async def test_dataset_scoped_arbitration_blocked_by_other_global_conflict() -> None:
    run = make_run(dataset_id=uuid4())
    repo = FakeClaimRepository(other_global_conflict=True)
    ok = await PipelineRunClaimer._arbitrate_dataset_scoped(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls[-1] == "other_global_conflict"


@pytest.mark.asyncio
async def test_global_arbitration_succeeds_when_all_clear() -> None:
    run = make_run(dataset_id=None)
    repo = FakeClaimRepository()
    ok = await PipelineRunClaimer._arbitrate_global(repo, run)  # type: ignore[arg-type]
    assert ok is True
    assert repo.calls == ["exclusive_lock", "any_dataset_scoped_conflict", "other_global_conflict"]


@pytest.mark.asyncio
async def test_global_arbitration_aborts_when_exclusive_lock_unavailable() -> None:
    run = make_run(dataset_id=None)
    repo = FakeClaimRepository(exclusive_global_lock_ok=False)
    ok = await PipelineRunClaimer._arbitrate_global(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls == ["exclusive_lock"]


@pytest.mark.asyncio
async def test_global_arbitration_blocked_by_dataset_scoped_conflict() -> None:
    run = make_run(dataset_id=None)
    repo = FakeClaimRepository(any_dataset_scoped_conflict=True)
    ok = await PipelineRunClaimer._arbitrate_global(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert "other_global_conflict" not in repo.calls  # short-circuits


@pytest.mark.asyncio
async def test_global_arbitration_blocked_by_other_global_conflict() -> None:
    run = make_run(dataset_id=None)
    repo = FakeClaimRepository(other_global_conflict=True)
    ok = await PipelineRunClaimer._arbitrate_global(repo, run)  # type: ignore[arg-type]
    assert ok is False
    assert repo.calls[-1] == "other_global_conflict"


# --- time authority (ADR-0009 SS 5): PostgreSQL, never the Python clock -----


def test_claimer_module_does_not_import_a_python_clock_authority() -> None:
    """The claim path must derive its persisted timestamps exclusively from
    PostgreSQL's own now() (repository.get_database_now()), never from an
    application-side clock. Absence of any Python-clock helper (utc_now,
    datetime.now, etc.) in this module's namespace is a cheap, non-flaky
    proof that complements the PostgreSQL-bounds integration test."""

    import sofias_memory.services.pipeline_queue_claimer as module

    assert not hasattr(module, "utc_now")
    assert not hasattr(module, "datetime")
