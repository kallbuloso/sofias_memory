"""Unit tests for the ADR-0011 D31/D43 process-state model (STORAGE-007)."""

from __future__ import annotations

from sofias_memory.services.process_state import (
    ClaimPolicy,
    ProcessState,
    ProcessStateHolder,
    claim_policy_for_state,
)


def test_initial_state_is_bootstrap_maintenance() -> None:
    holder = ProcessStateHolder()
    assert holder.state is ProcessState.BOOTSTRAP_MAINTENANCE
    assert holder.is_operational is False
    assert holder.claim_policy is ClaimPolicy.NONE


def test_transition_to_storage_converging_sets_recovery_only_policy() -> None:
    holder = ProcessStateHolder()
    holder.transition(ProcessState.STORAGE_CONVERGING)
    assert holder.state is ProcessState.STORAGE_CONVERGING
    assert holder.is_operational is False
    assert holder.claim_policy is ClaimPolicy.RECOVERY_ONLY


def test_transition_to_operational_sets_normal_policy() -> None:
    holder = ProcessStateHolder()
    holder.transition(ProcessState.OPERATIONAL)
    assert holder.is_operational is True
    assert holder.claim_policy is ClaimPolicy.NORMAL


def test_claim_policy_for_state_covers_every_state() -> None:
    assert claim_policy_for_state(ProcessState.BOOTSTRAP_MAINTENANCE) is ClaimPolicy.NONE
    assert claim_policy_for_state(ProcessState.STORAGE_CONVERGING) is ClaimPolicy.RECOVERY_ONLY
    assert claim_policy_for_state(ProcessState.OPERATIONAL) is ClaimPolicy.NORMAL


def test_holder_detail_is_recorded_on_transition() -> None:
    holder = ProcessStateHolder()
    holder.transition(ProcessState.STORAGE_CONVERGING, detail="probing s3")
    assert holder.detail == "probing s3"
