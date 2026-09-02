"""Process-local application lifecycle state (ADR-0011 D31, STORAGE-007).

Three closed states, in strict forward order:

    BOOTSTRAP_MAINTENANCE -> STORAGE_CONVERGING -> OPERATIONAL
    BOOTSTRAP_MAINTENANCE -> OPERATIONAL                        (filesystem mode)

This is deliberately **not** durable PostgreSQL/``PipelineRun`` state (D31) --
it is one process's own bootstrap progress, read-safe from concurrent async
request/worker code paths, never persisted, never a distributed coordinator.
D43's correctness argument rests on durable ``Source``/``PipelineRun`` state
plus the accepted single-process MVP deployment model, not on this holder.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

__all__ = [
    "ClaimPolicy",
    "ProcessState",
    "ProcessStateHolder",
    "claim_policy_for_state",
]


class ProcessState(StrEnum):
    BOOTSTRAP_MAINTENANCE = "bootstrap_maintenance"
    STORAGE_CONVERGING = "storage_converging"
    OPERATIONAL = "operational"


class ClaimPolicy(StrEnum):
    """What :class:`~sofias_memory.services.pipeline_queue_claimer.PipelineRunClaimer`
    may claim right now -- derived from :class:`ProcessState`, never persisted,
    never a second queue/engine (D31's "no second queue, no second engine")."""

    NONE = "none"
    """BOOTSTRAP_MAINTENANCE: claim nothing at all."""

    RECOVERY_ONLY = "recovery_only"
    """STORAGE_CONVERGING: only a ``forget``/``dataset_delete`` run whose own
    authoritative destructive mutation has already durably succeeded for its
    complete target scope (D31 sixth amendment) may be claimed."""

    NORMAL = "normal"
    """OPERATIONAL: ordinary ADR-0009 claim eligibility, unchanged."""


def claim_policy_for_state(state: ProcessState) -> ClaimPolicy:
    if state is ProcessState.OPERATIONAL:
        return ClaimPolicy.NORMAL
    if state is ProcessState.STORAGE_CONVERGING:
        return ClaimPolicy.RECOVERY_ONLY
    return ClaimPolicy.NONE


@dataclass
class ProcessStateHolder:
    """One process's own mutable, read-safe lifecycle state.

    Plain attribute reads/writes are safe here because this process runs a
    single asyncio event loop with no preemptive threading of application
    code (the same assumption every other process-local mutable coordinator
    in this codebase, e.g. ``PipelineWorkerCoordinator``, already relies on).

    Defaults to ``BOOTSTRAP_MAINTENANCE`` -- the correct fail-closed starting
    point for both a real process (``app.py``'s ``create_app`` now also
    defaults to this, closing a prior fail-open gap) and ``lifespan()``,
    which additionally force-resets this state explicitly at its own start
    regardless of what a caller passed in, so a construction-time default
    only ever matters for a holder nobody's lifespan ever drives (e.g. a test
    that never runs the ASGI lifespan protocol at all).
    """

    state: ProcessState = field(default=ProcessState.BOOTSTRAP_MAINTENANCE)
    detail: str | None = None
    recovery_owned_run_ids: frozenset[UUID] = field(default_factory=frozenset)
    """ADR-0011 D31 sixth amendment / final fail-closed audit: the most
    recent ``STORAGE_CONVERGING`` classification pass's own set of
    recovery-owned (Case B) ``PipelineRun`` ids -- an in-memory NARROWING
    input only (see :meth:`set_recovery_owned_run_ids`), never durable
    authority. A run's own ``pipeline_type`` and its authoritative-mutation
    step's persisted ``SUCCEEDED`` status remain the durable checks the claim
    path performs itself against PostgreSQL; this set exists only to prevent
    an unrelated destructive run that happens to be past its own
    authoritative mutation, but whose scope was never actually classified as
    Case B, from being claimed merely because ``pipeline_type`` and
    authoritative-mutation-succeeded alone would otherwise allow it."""

    @property
    def claim_policy(self) -> ClaimPolicy:
        return claim_policy_for_state(self.state)

    @property
    def is_operational(self) -> bool:
        return self.state is ProcessState.OPERATIONAL

    def transition(self, state: ProcessState, *, detail: str | None = None) -> None:
        self.state = state
        self.detail = detail

    def set_recovery_owned_run_ids(self, run_ids: Iterable[UUID]) -> None:
        """Replace the current narrowing set wholesale with the ids from the
        latest convergence classification pass -- never merged/accumulated,
        so a run no longer classified Case B in the newest pass stops being
        claimable on the very next attempt, even if an earlier pass once
        included it."""

        self.recovery_owned_run_ids = frozenset(run_ids)
