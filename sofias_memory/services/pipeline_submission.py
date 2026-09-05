"""Shared durable submission contract for B5 write endpoints (ADR-0009 SS C/
SS J/SS S, SM-509).

One authority for "accept a write request as a durable PipelineRun", reused
by every B5-migrated endpoint (SM-510..SM-513, SM-515) instead of each one
reimplementing idempotency resolution, the transactional preparation hook,
and the worker-availability gate. This module has no FastAPI dependency and
knows nothing about any specific pipeline's business result shape -- that is
each consuming story's job (ADR-0009 SS R point 4).

Transaction boundary: :meth:`PipelineSubmissionService.submit` either
resolves an existing ``PipelineRun`` (read-only, no mutation) or performs the
full ADR-0009 SS C sequence -- transactional preparation, ``PipelineRun`` +
every ``PipelineStep`` row, commit -- as one atomic unit. No caller ever
observes a run accepted before that commit.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import TERMINAL_RUN_STATUSES, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep, Session
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.hashing import canonical_work_payload_hash
from sofias_memory.pipelines.registry import PipelineRegistry, StepPlan
from sofias_memory.schemas.common import ErrorCode, JSONValue
from sofias_memory.services.dataset_delete_barrier import raise_if_dataset_administratively_blocked
from sofias_memory.services.pipeline_lifecycle import create_run_with_steps


class PipelineRunRepositoryForSubmission(Protocol):
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...
    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None: ...
    async def add(self, run: PipelineRun) -> PipelineRun: ...


class PipelineStepRepositoryForSubmission(Protocol):
    async def add_many(self, steps: list[PipelineStep]) -> list[PipelineStep]: ...


class SessionRepositoryForSubmission(Protocol):
    """SM-605: the slice a ``PreparationHook`` needs to resolve/lazily
    create and lock a Session inside the same submission transaction as the
    ``PipelineRun`` it will be attached to -- structurally identical to
    ``SessionRepositoryForRecall`` (``services.recall``), reused by name
    only, not by import, to keep this module's Protocol surface self
    contained."""

    async def get_or_create_by_key(self, candidate: Session) -> Session: ...
    async def get_by_id_for_update(self, session_id: UUID) -> Session | None: ...


class SubmissionUnitOfWork(Protocol):
    """Structurally identical to the slice of ``PostgresUnitOfWork`` that
    :func:`~sofias_memory.services.pipeline_lifecycle.create_run_with_steps`
    and this module actually use -- lets unit tests exercise the full
    submission algorithm against an in-memory fake, while production and
    integration tests use the real ``PostgresUnitOfWork`` (same pattern as
    ``DatasetService``/``RunService``, SM-421/SM-508)."""

    pipeline_runs: PipelineRunRepositoryForSubmission
    pipeline_steps: PipelineStepRepositoryForSubmission
    sessions: SessionRepositoryForSubmission

    async def __aenter__(self) -> SubmissionUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], SubmissionUnitOfWork]

RESERVED_IDEMPOTENCY_KEY_PREFIX = "sys:"
"""ADR-0009 SS M: reserved for internal mechanisms (manual retry, and any
future internal use) -- a client-supplied ``Idempotency-Key`` starting with
this prefix is rejected before any lookup/insert, so an internal key can
never collide with, or be forged/hijacked via, a caller-supplied one."""

IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "uq_pipeline_runs_idempotency_key"
"""The one PostgreSQL constraint whose violation may ever be reinterpreted
as an idempotency race (SM-509 audit Finding 2). ``Idempotency-Key`` is
global across the whole ``pipeline_runs`` table (this partial unique index
has no ``pipeline_type`` in its predicate), so an ``IntegrityError`` from any
OTHER constraint (e.g. a broken ``prepare()`` hook inserting a bad foreign
key) must always propagate unchanged -- even if, coincidentally, a run for
that same key already exists by the time the failure is inspected."""


def _is_idempotency_key_unique_violation(error: IntegrityError) -> bool:
    """Whether ``error`` was caused specifically by
    :data:`IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT` -- read safely off the
    underlying DBAPI exception's ``constraint_name``, never guessed from the
    error message string.

    SQLAlchemy's asyncpg dialect wraps the real ``asyncpg.exceptions.
    UniqueViolationError`` (which carries ``constraint_name``, populated by
    asyncpg from PostgreSQL's own error fields) inside its own DBAPI-style
    exception class and re-raises it with ``raise translated_error from
    error`` -- so ``constraint_name`` lives on ``error.orig.__cause__``, not
    directly on ``error.orig`` itself. Both are checked defensively (``or``)
    so this keeps working if a future SQLAlchemy/asyncpg version stops
    wrapping, without ever falling back to string-matching the message."""

    orig = error.orig
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return constraint_name == IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT


@dataclass(frozen=True, slots=True)
class SubmissionTargets:
    """What a :data:`PreparationHook` resolves inside the submission
    transaction, before the ``PipelineRun`` row is inserted (ADR-0009 SS C
    step 2): the authoritative target identity for this pipeline type. Not
    every pipeline type needs both -- an operation with no dataset scope
    (a true global run) passes ``dataset_id=None``."""

    dataset_id: UUID | None
    source_id: UUID | None
    session_id: UUID | None = None
    """SM-605: the authoritative Session FK for this operation (``PipelineRun.
    session_id``), resolved/locked by the hook itself inside this same
    transaction -- ``None`` for every pipeline type that has no Session
    concept. Defaults to ``None`` so no unrelated hook needs updating."""


PreparationHook = Callable[[SubmissionUnitOfWork], Coroutine[None, None, SubmissionTargets]]
"""``(uow) -> SubmissionTargets``, run inside the same transaction as the new
``PipelineRun``/``PipelineStep`` rows, before they are inserted (ADR-0009 SS
C step 2). Must be PostgreSQL-only: no commit of its own, no LLM/embedding/
HTTP/Neo4j/filesystem call. A losing idempotency race (SS G) rolls this hook's
work back along with everything else in the same transaction -- it must
never have an external side effect that a rollback cannot undo, which is
exactly why external I/O is forbidden here rather than merely discouraged.

Every :meth:`PipelineSubmissionService.submit` call supplies one, even when
trivial (e.g. a pipeline type with no dataset resolution to perform simply
returns the caller-supplied ids unchanged) -- there is deliberately no
"skip the hook" code path, so target resolution is always exercised inside
the same transaction as the run it targets (SM-509 Part C).

**Concurrency contract (SM-509 audit Finding: PreparationHook responsibility
boundary).** If the hook resolves, creates, or mutates authoritative state
protected by its OWN uniqueness or concurrency constraint -- for example, a
future lazy "get-or-create Dataset by slug" hook racing against
``Dataset.slug``'s unique constraint when two concurrent submissions both
see the slug as not-yet-existing -- the hook itself must handle races on
that target safely. Acceptable patterns, chosen per operation:

- ``INSERT ... ON CONFLICT ...`` (upsert);
- get-or-create that catches and recovers from its OWN specific
  ``IntegrityError``/constraint, then re-reads the now-committed-by-the-
  winner row;
- a retry/re-read loop scoped to that operation's own semantics;
- PostgreSQL-specific locking, only when genuinely required by that
  target's semantics (not a default to reach for).

:class:`PipelineSubmissionService` is responsible ONLY for the race on its
OWN submission identity, :data:`IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT`
(``uq_pipeline_runs_idempotency_key``) -- it deliberately does NOT, and must
not, generically reinterpret an arbitrary ``IntegrityError`` raised from
inside a hook (e.g. a target's own unique-constraint violation) as an
idempotency-key race. A hook must never depend on the submitter to recover
uniqueness conflicts belonging to the resource *it* prepares -- that
recovery is the hook's own contract to fulfill, using whichever pattern
above fits the target it owns.
"""


class WorkerAvailability(Protocol):
    """The minimal worker-availability signal SM-509 needs (ADR-0009 SS U):
    already implemented by ``PipelineWorkerCoordinator`` -- this Protocol
    exists only to decouple the submission service from that concrete class,
    not to introduce a second readiness framework.

    ``is_operational`` (SM-516 staging fix) is the single source of truth
    for "may this worker be handed NEW work" -- the same predicate
    ``/health/ready`` uses, so a dead poll/outbox task blocks new-run
    creation exactly as it blocks readiness. Never re-derive that predicate
    here; read the one signal."""

    @property
    def enabled(self) -> bool: ...

    @property
    def is_operational(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """Plain-data copy of the ``PipelineRun`` fields this module needs,
    captured while its owning session/transaction is still open.

    Never the live ORM instance itself: a ``PostgresUnitOfWork`` closes its
    session on ``__aexit__``, which detaches any ORM object obtained from it
    -- accessing an attribute on a detached instance after that point raises
    ``sqlalchemy.orm.exc.DetachedInstanceError`` instead of silently working
    (SQLAlchemy's default ``expire_on_commit``/close-time expiry). Every
    lookup helper below extracts this snapshot before its ``async with``
    block exits, precisely to avoid ever handing a detached instance to a
    caller outside that block.
    """

    id: UUID
    pipeline_type: PipelineType
    dataset_id: UUID | None
    source_id: UUID | None
    session_id: UUID | None
    status: PipelineRunStatus
    payload_hash: str
    input: Mapping[str, JSONValue]
    retry_of_run_id: UUID | None


def _snapshot(run: PipelineRun) -> _RunSnapshot:
    return _RunSnapshot(
        id=run.id,
        pipeline_type=run.pipeline_type,
        dataset_id=run.dataset_id,
        source_id=run.source_id,
        session_id=run.session_id,
        status=run.status,
        payload_hash=run.payload_hash,
        input=cast("Mapping[str, JSONValue]", run.input),
        retry_of_run_id=run.retry_of_run_id,
    )


LegacyIntentEquivalent = Callable[[Mapping[str, JSONValue], Mapping[str, JSONValue]], bool]
"""``(existing_run_input, new_work_input) -> bool``. An optional, narrow
per-call extension to the idempotency-key resolution match (SM-513 SS 6):
when a run's persisted ``payload_hash`` does not equal the new submission's
hash, this callable gets one more chance to decide the two are still the
SAME semantic work -- e.g. Remember's B4-legacy compatibility, where a
historical B4 ``PipelineRun.input`` included ``wait`` (a field B5's own
``payload_hash`` never covers). Never applied to the ``pipeline_type`` check
-- a mismatched pipeline type is always a conflict, regardless of this
callable. Every OTHER pipeline type passes ``None`` (the default), which
keeps this codepath byte-for-byte identical to its pre-SM-513 behavior."""


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What a durable submission resolved to -- enough for a future route
    (SM-510..SM-513/SM-515) to build its ``202``/terminal-result response,
    without this shared layer knowing any endpoint-specific result shape
    (ADR-0009 SS R point 4)."""

    run_id: UUID
    pipeline_type: PipelineType
    dataset_id: UUID | None
    source_id: UUID | None
    status: PipelineRunStatus
    created: bool
    session_id: UUID | None = None
    """SM-605: the authoritative ``PipelineRun.session_id`` -- for a replay
    (``created=False``), the ORIGINAL run's persisted association, never
    re-resolved from the new request's own ``session_id``."""
    """``True``: a brand-new ``PipelineRun`` was just committed. ``False``:
    an existing run was resolved via ``Idempotency-Key`` (any status,
    including terminal safe-replay)."""
    retry_of_run_id: UUID | None = None
    """Set when this run was created (or resolved) as a manual retry
    (SM-514): the original run's id. ``None`` for a normal submission."""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


def reserved_idempotency_key_namespace_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.RESERVED_IDEMPOTENCY_KEY_NAMESPACE,
        status_code=HTTPStatus.BAD_REQUEST,
        message="Idempotency-Key uses a namespace reserved for internal use.",
        details={"reserved_prefix": RESERVED_IDEMPOTENCY_KEY_PREFIX},
    )


def idempotency_conflict_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.IDEMPOTENCY_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message="Idempotency-Key was already used for different work.",
    )


def worker_disabled_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.WORKER_DISABLED,
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        message="The pipeline worker is unavailable; this operation cannot be accepted.",
    )


class PipelineSubmissionService:
    """Single authority for durable B5 submission (ADR-0009 SS C/SS S/SS U).

    Read-only observation and durable creation are the only two outcomes.
    Never executes a pipeline step, never talks to Neo4j, never consults the
    worker beyond the injected :class:`WorkerAvailability` readiness signal.
    """

    def __init__(
        self,
        *,
        registry: PipelineRegistry,
        worker: WorkerAvailability,
        config_fingerprint: str,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )
        self._registry = registry
        self._worker = worker
        self._config_fingerprint = config_fingerprint

    async def submit(
        self,
        *,
        pipeline_type: PipelineType,
        work_input: Mapping[str, JSONValue],
        idempotency_key: str | None,
        prepare: PreparationHook,
        run_id: UUID | None = None,
        legacy_intent_equivalent: LegacyIntentEquivalent | None = None,
    ) -> SubmissionOutcome:
        """Public entry point: rejects a client-supplied ``sys:``-prefixed
        key outright (ADR-0009 SS M). Every public write route calls this,
        never :meth:`submit_trusted_internal`."""

        if idempotency_key is not None and idempotency_key.startswith(
            RESERVED_IDEMPOTENCY_KEY_PREFIX
        ):
            raise reserved_idempotency_key_namespace_error()
        return await self._submit(
            pipeline_type=pipeline_type,
            work_input=work_input,
            idempotency_key=idempotency_key,
            prepare=prepare,
            run_id=run_id,
            legacy_intent_equivalent=legacy_intent_equivalent,
            retry_of_run_id=None,
        )

    async def submit_trusted_internal(
        self,
        *,
        pipeline_type: PipelineType,
        work_input: Mapping[str, JSONValue],
        idempotency_key: str,
        prepare: PreparationHook,
        run_id: UUID | None = None,
        retry_of_run_id: UUID | None = None,
    ) -> SubmissionOutcome:
        """Internal-only entry point (SM-514 SS 21): the sole caller
        permitted to submit under the reserved ``sys:`` idempotency-key
        namespace -- currently manual retry
        (``sys:retry:{original_run_id}``). Never reachable from a public
        route; ``idempotency_key`` must already carry the reserved prefix
        (enforced defensively, never relaxed to accept an arbitrary key)."""

        if not idempotency_key.startswith(RESERVED_IDEMPOTENCY_KEY_PREFIX):
            raise ValueError(
                "submit_trusted_internal requires a reserved-namespace idempotency_key"
            )
        return await self._submit(
            pipeline_type=pipeline_type,
            work_input=work_input,
            idempotency_key=idempotency_key,
            prepare=prepare,
            run_id=run_id,
            legacy_intent_equivalent=None,
            retry_of_run_id=retry_of_run_id,
        )

    async def _submit(
        self,
        *,
        pipeline_type: PipelineType,
        work_input: Mapping[str, JSONValue],
        idempotency_key: str | None,
        prepare: PreparationHook,
        run_id: UUID | None,
        legacy_intent_equivalent: LegacyIntentEquivalent | None,
        retry_of_run_id: UUID | None,
    ) -> SubmissionOutcome:
        payload_hash = canonical_work_payload_hash(work_input)

        if idempotency_key is not None:
            existing = await self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                # Existing-run resolution is a pure replay (SM-509 audit
                # Finding 3/5): no worker-availability gate, no registry
                # lookup, no step plan, no prepare() -- none of that is
                # needed to observe a run that already exists, in ANY of the
                # six statuses, so none of it may ever block this path.
                return self._resolve_existing(
                    pipeline_type,
                    existing,
                    payload_hash=payload_hash,
                    work_input=work_input,
                    legacy_intent_equivalent=legacy_intent_equivalent,
                )

        # Only a genuinely new PipelineRun reaches this point -- everything
        # below is exclusive to creating new state.
        self._require_worker_available()

        # Pure, no I/O: validates registration and derives the durable step
        # plan from the SAME registry the worker executes against (SM-509
        # Part J) -- an unregistered PipelineType raises here, before any
        # PostgreSQL access, so it can never create a phantom run.
        step_plan = self._registry.build_step_plan(pipeline_type, run_input=work_input)

        try:
            return await self._create_new_run(
                pipeline_type=pipeline_type,
                work_input=work_input,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
                prepare=prepare,
                step_plan=step_plan,
                run_id=run_id,
                retry_of_run_id=retry_of_run_id,
            )
        except IntegrityError as error:
            if idempotency_key is None or not _is_idempotency_key_unique_violation(error):
                # No key means no possible idempotency race on this insert
                # (SM-509 Part G); and a failure from any constraint OTHER
                # than the idempotency-key unique index is never an
                # idempotency race no matter what a fresh lookup happens to
                # find afterward (SM-509 audit Finding 2) -- always
                # propagate unchanged.
                raise
            existing = await self._find_by_idempotency_key(idempotency_key)
            if existing is None:
                # The failure was not this key racing with itself -- re-raise
                # the original error rather than fabricating a conflict.
                raise
            return self._resolve_existing(
                pipeline_type,
                existing,
                payload_hash=payload_hash,
                work_input=work_input,
                legacy_intent_equivalent=legacy_intent_equivalent,
            )

    def _resolve_existing(
        self,
        pipeline_type: PipelineType,
        existing: _RunSnapshot,
        *,
        payload_hash: str,
        work_input: Mapping[str, JSONValue],
        legacy_intent_equivalent: LegacyIntentEquivalent | None,
    ) -> SubmissionOutcome:
        # ADR-0009 SS S's mismatch guard is two-part, not one (SM-509 audit
        # Finding 1): `Idempotency-Key` is GLOBAL across pipeline_runs (the
        # partial unique index has no pipeline_type in its predicate), so a
        # matching payload_hash alone never proves it is the same work --
        # the same key reused for a different pipeline_type is exactly the
        # kind of accidental reuse this guard exists to catch. pipeline_type
        # is deliberately compared explicitly rather than folded into the
        # hash, to keep payload_hash byte-compatible with B4 (Finding 4).
        # pipeline_type is never overridden by the legacy matcher -- only a
        # payload_hash mismatch gets a second chance (SM-513 SS 6).
        if existing.pipeline_type != pipeline_type:
            raise idempotency_conflict_error()
        if existing.payload_hash != payload_hash and (
            legacy_intent_equivalent is None
            or not legacy_intent_equivalent(existing.input, work_input)
        ):
            raise idempotency_conflict_error()

        return SubmissionOutcome(
            run_id=existing.id,
            pipeline_type=existing.pipeline_type,
            dataset_id=existing.dataset_id,
            source_id=existing.source_id,
            status=existing.status,
            created=False,
            session_id=existing.session_id,
            retry_of_run_id=existing.retry_of_run_id,
        )

    async def _create_new_run(
        self,
        *,
        pipeline_type: PipelineType,
        work_input: Mapping[str, JSONValue],
        payload_hash: str,
        idempotency_key: str | None,
        prepare: PreparationHook,
        step_plan: list[StepPlan],
        run_id: UUID | None,
        retry_of_run_id: UUID | None,
    ) -> SubmissionOutcome:
        async with self._unit_of_work_factory() as uow:
            targets = await prepare(uow)
            if pipeline_type != PipelineType.DATASET_DELETE:
                # ADR-0010 D12/D16: the delete-intent barrier applies to
                # every OTHER dataset-scoped pipeline type's new-run
                # creation -- including a manual retry's own
                # submit_trusted_internal call, which reaches this same
                # code path. DATASET_DELETE's own submission never goes
                # through this generic service at all (see
                # services.dataset_delete), so it is never blocked by its
                # own barrier.
                await raise_if_dataset_administratively_blocked(
                    cast(PostgresUnitOfWork, uow), targets.dataset_id
                )
            run = await create_run_with_steps(
                cast(PostgresUnitOfWork, uow),
                pipeline_type=pipeline_type,
                dataset_id=targets.dataset_id,
                source_id=targets.source_id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                input=dict(work_input),
                config_fingerprint=self._config_fingerprint,
                steps=step_plan,
                run_id=run_id,
                retry_of_run_id=retry_of_run_id,
                session_id=targets.session_id,
            )
            await uow.commit()
            new_run_id = run.id

        # A fresh, independent read (ADR-0009 SS Q/SM-509 Part L): the
        # in-memory object we just committed is always QUEUED/attempt=0 by
        # construction, but the worker may have already claimed it by the
        # time this line runs (a benign race, SS Q point 2) -- report
        # whatever PostgreSQL now says, never force `queued` artificially.
        current = await self._find_by_id(new_run_id)
        assert current is not None  # noqa: S101 - just committed, cannot vanish
        return SubmissionOutcome(
            run_id=current.id,
            pipeline_type=current.pipeline_type,
            dataset_id=current.dataset_id,
            source_id=current.source_id,
            status=current.status,
            created=True,
            session_id=current.session_id,
            retry_of_run_id=current.retry_of_run_id,
        )

    async def _find_by_idempotency_key(self, idempotency_key: str) -> _RunSnapshot | None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_idempotency_key(idempotency_key)
            return _snapshot(run) if run is not None else None

    async def _find_by_id(self, run_id: UUID) -> _RunSnapshot | None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            return _snapshot(run) if run is not None else None

    def _require_worker_available(self) -> None:
        if not (self._worker.enabled and self._worker.is_operational):
            raise worker_disabled_error()


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> SubmissionUnitOfWork:
        return cast(SubmissionUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
