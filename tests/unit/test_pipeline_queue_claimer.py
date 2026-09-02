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
from sofias_memory.services.process_state import ClaimPolicy

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


# ---------------------------------------------------------------------------
# ADR-0011 D31/D43 (STORAGE-007): recovery-owned claim eligibility.
# Implements validation items 67-69 at the claim-predicate level.
# ---------------------------------------------------------------------------


@dataclass
class FakeAuthoritativeMutationRepository:
    succeeded: bool = False
    calls: list[tuple[UUID, PipelineType]] = field(default_factory=list)

    async def authoritative_mutation_succeeded(
        self, run_id: UUID, *, pipeline_type: PipelineType
    ) -> bool:
        self.calls.append((run_id, pipeline_type))
        return self.succeeded


@pytest.mark.asyncio
async def test_normal_policy_allows_any_pipeline_type_unconditionally() -> None:
    repo = FakeAuthoritativeMutationRepository(succeeded=False)
    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.REMEMBER

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.NORMAL, recovery_owned_run_ids=frozenset()
    )

    assert allowed is True
    assert repo.calls == []  # never even consulted -- NORMAL never needs it


@pytest.mark.asyncio
async def test_none_policy_never_allows_anything() -> None:
    repo = FakeAuthoritativeMutationRepository(succeeded=True)
    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.NONE, recovery_owned_run_ids=frozenset({run.id})
    )

    assert allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pipeline_type", [PipelineType.REMEMBER, PipelineType.COGNIFY, PipelineType.IMPROVE]
)
async def test_recovery_only_policy_rejects_non_destructive_pipeline_types(
    pipeline_type: PipelineType,
) -> None:
    repo = FakeAuthoritativeMutationRepository(succeeded=True)
    run = make_run(dataset_id=None)
    run.pipeline_type = pipeline_type

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo,
        run,
        policy=ClaimPolicy.RECOVERY_ONLY,
        # Case-B membership present too -- pipeline_type alone must still be
        # decisive, proving the checks are independent, not merely additive.
        recovery_owned_run_ids=frozenset({run.id}),
    )

    assert allowed is False
    assert repo.calls == []  # never even consulted -- wrong pipeline_type is decisive alone


@pytest.mark.asyncio
async def test_recovery_only_policy_rejects_run_not_in_recovery_owned_set() -> None:
    """Final fail-closed audit -- the missing negative test: R1, an unrelated
    destructive run that is past its own authoritative mutation but whose
    scope was never classified Case B by convergence, must NOT be claimable.
    Pipeline type + authoritative-mutation-succeeded alone is necessary but
    NOT sufficient -- Case-B membership (here: absence from the narrowing
    set) is independently required."""

    repo = FakeAuthoritativeMutationRepository(succeeded=True)
    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.RECOVERY_ONLY, recovery_owned_run_ids=frozenset()
    )

    assert allowed is False
    # Short-circuits before even consulting the durable authoritative-
    # mutation check -- membership is checked first (cheaper, in-memory).
    assert repo.calls == []


@pytest.mark.asyncio
async def test_recovery_only_policy_rejects_forget_with_unsucceeded_mutation() -> None:
    """D31 sixth amendment: Case-B membership alone (or pipeline_type alone,
    tested above) is never sufficient -- a pre-authoritative-mutation Forget
    run must never be claimable even when its scope is genuinely Case B."""

    repo = FakeAuthoritativeMutationRepository(succeeded=False)
    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.RECOVERY_ONLY, recovery_owned_run_ids=frozenset({run.id})
    )

    assert allowed is False
    assert repo.calls == [(run.id, PipelineType.FORGET)]


@pytest.mark.asyncio
async def test_recovery_only_policy_allows_forget_with_succeeded_mutation() -> None:
    """R2: a genuinely Case-B-classified Forget run, past its own
    authoritative mutation, IS claimable -- the same worker/engine as
    OPERATIONAL, no second execution path."""

    repo = FakeAuthoritativeMutationRepository(succeeded=True)
    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.RECOVERY_ONLY, recovery_owned_run_ids=frozenset({run.id})
    )

    assert allowed is True


@pytest.mark.asyncio
async def test_recovery_only_policy_allows_dataset_delete_with_succeeded_mutation() -> None:
    """Validation item 68: the DATASET_DELETE multi-source case -- once the
    run's own deactivate_authoritative step has durably succeeded for its
    complete scope AND convergence classified this run's scope as Case B,
    the run is claim-eligible."""

    repo = FakeAuthoritativeMutationRepository(succeeded=True)
    run = make_run(dataset_id=uuid4())
    run.pipeline_type = PipelineType.DATASET_DELETE

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.RECOVERY_ONLY, recovery_owned_run_ids=frozenset({run.id})
    )

    assert allowed is True
    assert repo.calls == [(run.id, PipelineType.DATASET_DELETE)]


@pytest.mark.asyncio
async def test_recovery_only_policy_rejects_dataset_delete_before_mutation_succeeds() -> None:
    """Validation item 67: a DATASET_DELETE run targeting a Dataset with one
    Case-B Source and one still-live Case-A Source must NOT be claimable
    while its own deactivate_authoritative step has not yet durably
    succeeded for the dataset's complete scope -- claiming it here would
    risk executing a new ACTIVE -> DELETING mutation during
    STORAGE_CONVERGING, exactly the race D43 forbids. Case-B membership is
    present (the run's scope does include a Case-B source) -- it is the
    unsucceeded authoritative-mutation check that must still block it."""

    repo = FakeAuthoritativeMutationRepository(succeeded=False)
    run = make_run(dataset_id=uuid4())
    run.pipeline_type = PipelineType.DATASET_DELETE

    allowed = await PipelineRunClaimer._allowed_under_policy(  # type: ignore[arg-type]
        repo, run, policy=ClaimPolicy.RECOVERY_ONLY, recovery_owned_run_ids=frozenset({run.id})
    )

    assert allowed is False


@dataclass
class _OrderingRepository:
    """Proves claim-operation ordering: discover -> validate recovery policy
    -> ONLY THEN any ownership-changing write (arbitration lock, commit).
    Every arbitration/commit-adjacent method raises if ever reached -- this
    run's policy check is configured to fail, so if the implementation ever
    claimed first and validated after, one of these would fire instead of the
    expected clean rejection."""

    run: PipelineRun
    calls: list[str] = field(default_factory=list)

    async def get_eligible_for_update(self, candidate_id: UUID) -> PipelineRun | None:
        self.calls.append("get_eligible_for_update")
        return self.run

    async def authoritative_mutation_succeeded(
        self, run_id: UUID, *, pipeline_type: PipelineType
    ) -> bool:
        self.calls.append("authoritative_mutation_succeeded")
        return True  # would allow the claim if the ordering were wrong

    async def try_advisory_lock_shared(self, key: int) -> bool:
        raise AssertionError("must not attempt any lock before the policy check rejects")

    async def try_advisory_lock_exclusive(self, key: int) -> bool:
        raise AssertionError("must not attempt any lock before the policy check rejects")

    async def get_database_now(self) -> datetime:
        raise AssertionError("must not read a claim timestamp before the policy check rejects")

    async def list_eligible_candidate_ids(self, *, limit: int) -> list[UUID]:
        return [self.run.id]


class _OrderingUow:
    def __init__(self, repo: _OrderingRepository) -> None:
        self.pipeline_runs = repo

    async def __aenter__(self) -> _OrderingUow:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def commit(self) -> None:
        raise AssertionError("must not commit a run whose recovery policy check failed")


@pytest.mark.asyncio
async def test_try_claim_one_validates_recovery_policy_before_any_ownership_changing_write() -> (
    None
):
    """Claim atomicity/ownership: discover -> validate -> claim, never claim
    -> validate. A run rejected by RECOVERY_ONLY (not in the narrowing set,
    here) must never reach arbitration locking, PostgreSQL's claim-time
    now(), or commit -- ``_OrderingRepository``'s corresponding methods
    would raise if the implementation reached them."""

    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET
    repo = _OrderingRepository(run=run)

    import sofias_memory.services.pipeline_queue_claimer as claimer_module

    original_uow = claimer_module.PostgresUnitOfWork
    claimer_module.PostgresUnitOfWork = lambda *_a, **_k: _OrderingUow(repo)  # type: ignore[assignment]
    try:
        claimer = PipelineRunClaimer(
            session_factory=lambda: None,  # type: ignore[arg-type]
            claim_policy=lambda: ClaimPolicy.RECOVERY_ONLY,
            recovery_owned_run_ids=lambda: frozenset(),  # run.id NOT in the set
        )
        result = await claimer.try_claim_one(worker_id="wk-test")
    finally:
        claimer_module.PostgresUnitOfWork = original_uow

    assert result is None
    assert repo.calls == ["get_eligible_for_update"]  # rejected immediately after


@dataclass
class _RecoveryClaimRepository:
    """A minimal, fully-cooperative fake for a genuinely recovery-owned run
    claimed end to end through the real ``PipelineRunClaimer`` -- proves
    validation items 12/13 (valid Forget/DatasetDelete recovery-owned claim)
    using the exact same claim path OPERATIONAL uses, not a second one."""

    run: PipelineRun
    calls: list[str] = field(default_factory=list)
    committed: bool = False

    async def get_eligible_for_update(self, candidate_id: UUID) -> PipelineRun | None:
        self.calls.append("get_eligible_for_update")
        return self.run

    async def authoritative_mutation_succeeded(
        self, run_id: UUID, *, pipeline_type: PipelineType
    ) -> bool:
        self.calls.append("authoritative_mutation_succeeded")
        return True

    async def try_advisory_lock_shared(self, key: int) -> bool:
        self.calls.append("shared_lock")
        return True

    async def try_advisory_lock_exclusive(self, key: int) -> bool:
        self.calls.append("exclusive_lock")
        return True

    async def exists_dataset_conflict(self, dataset_id: UUID) -> bool:
        self.calls.append("dataset_conflict")
        return False

    async def exists_any_dataset_scoped_conflict(self) -> bool:
        self.calls.append("any_dataset_scoped_conflict")
        return False

    async def exists_other_global_conflict(self, *, exclude_run_id: UUID) -> bool:
        self.calls.append("other_global_conflict")
        return False

    async def exists_eligible_global_with_precedence(
        self, *, before_created_at: datetime, before_id: UUID
    ) -> bool:
        self.calls.append("fairness")
        return False

    async def get_database_now(self) -> datetime:
        self.calls.append("get_database_now")
        return NOW

    async def list_eligible_candidate_ids(self, *, limit: int) -> list[UUID]:
        return [self.run.id]


class _RecoveryClaimUow:
    def __init__(self, repo: _RecoveryClaimRepository) -> None:
        self.pipeline_runs = repo

    async def __aenter__(self) -> _RecoveryClaimUow:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def commit(self) -> None:
        self.pipeline_runs.committed = True


async def _claim_via_fake_repository(repo: _RecoveryClaimRepository) -> ClaimedRun | None:
    import sofias_memory.services.pipeline_queue_claimer as claimer_module

    original_uow = claimer_module.PostgresUnitOfWork
    claimer_module.PostgresUnitOfWork = lambda *_a, **_k: _RecoveryClaimUow(repo)  # type: ignore[assignment]
    try:
        claimer = PipelineRunClaimer(
            session_factory=lambda: None,  # type: ignore[arg-type]
            claim_policy=lambda: ClaimPolicy.RECOVERY_ONLY,
            recovery_owned_run_ids=lambda: frozenset({repo.run.id}),
        )
        return await claimer.try_claim_one(worker_id="wk-test")
    finally:
        claimer_module.PostgresUnitOfWork = original_uow


@pytest.mark.asyncio
async def test_try_claim_one_claims_a_genuine_recovery_owned_forget_run() -> None:
    """Validation item 12: a Case-B-classified, authoritative-mutation-
    succeeded FORGET run is claimed end to end (arbitration through commit)
    through the unmodified claim path -- the same one OPERATIONAL uses."""

    run = make_run(dataset_id=None)
    run.pipeline_type = PipelineType.FORGET
    repo = _RecoveryClaimRepository(run=run)

    claimed = await _claim_via_fake_repository(repo)

    assert claimed is not None
    assert claimed.run_id == run.id
    assert repo.committed is True


@pytest.mark.asyncio
async def test_try_claim_one_claims_a_genuine_recovery_owned_dataset_delete_run() -> None:
    """Validation item 13: a Case-B-classified, authoritative-mutation-
    succeeded DATASET_DELETE run is likewise claimed end to end through the
    dataset-scoped arbitration path."""

    run = make_run(dataset_id=uuid4())
    run.pipeline_type = PipelineType.DATASET_DELETE
    repo = _RecoveryClaimRepository(run=run)

    claimed = await _claim_via_fake_repository(repo)

    assert claimed is not None
    assert claimed.run_id == run.id
    assert repo.committed is True


@pytest.mark.asyncio
async def test_try_claim_one_scans_nothing_under_none_policy() -> None:
    """BOOTSTRAP_MAINTENANCE: zero claims, and not even a discovery scan --
    validation item covering test list item 13."""

    class _ExplodingRepository:
        async def list_eligible_candidate_ids(self, *, limit: int) -> list[UUID]:
            raise AssertionError("must never scan for candidates under ClaimPolicy.NONE")

    class _ExplodingUow:
        def __init__(self) -> None:
            self.pipeline_runs = _ExplodingRepository()

        async def __aenter__(self) -> _ExplodingUow:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    claimer = PipelineRunClaimer(
        session_factory=lambda: None,  # type: ignore[arg-type]
        claim_policy=lambda: ClaimPolicy.NONE,
    )

    import sofias_memory.services.pipeline_queue_claimer as claimer_module

    original_uow = claimer_module.PostgresUnitOfWork
    claimer_module.PostgresUnitOfWork = lambda *_a, **_k: _ExplodingUow()  # type: ignore[assignment]
    try:
        result = await claimer.try_claim_one(worker_id="wk-test")
    finally:
        claimer_module.PostgresUnitOfWork = original_uow

    assert result is None
