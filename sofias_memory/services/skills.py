"""Skill/SkillRevision persistence primitives and management service
(ADR-0013, SM-701/SM-702).

Two layers, kept in one module because SM-702's service is a thin
orchestration shell directly on top of SM-701's primitives:

1. Persistence primitives (SM-701, unchanged) -- pure, reusable by SM-702's
   ``SkillService`` and a future SM-703 (SKILL.md import/export). No
   FastAPI/HTTP-status/ErrorCode concerns; only decides *created vs.
   replayed vs. conflict* (Feature Contract SS 9.1/17).
2. ``SkillService`` (SM-702) -- the public management service backing
   ``api/routes/skills.py``. Computes the embedding text and calls the
   embedding provider itself, entirely outside any PostgreSQL transaction
   (Feature Contract SS 7.1), then opens a UoW to call the primitives
   above. Translates the primitives' plain exceptions into
   :class:`~sofias_memory.api.errors.SofiasMemoryError`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    SkillRevisionContent,
    SkillStatus,
    build_skill_resolution_text,
    compute_content_sha256,
)
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillListResult,
    SkillResult,
    SkillRevisionCreateRequest,
    SkillRevisionListItem,
    SkillRevisionListResult,
    SkillRevisionResult,
    SkillUpdateRequest,
)

SKILL_NAME_UNIQUE_CONSTRAINT = "uq_skills_name"
"""The one PostgreSQL constraint whose violation may ever be reinterpreted
as a Skill-name conflict (mirrors
``pipeline_submission.IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT``). An
``IntegrityError`` from any OTHER constraint must always propagate
unchanged."""


class SkillNameAlreadyExistsError(ValueError):
    """A Skill with this ``name`` already exists, in any status.

    Skill creation never upserts and never resolves by content hash --
    identity is decided solely by ``name`` (Feature Contract SS 6.1/12.4).
    ``content_sha256``-based safe replay applies only to creating a
    *revision* on an already-identified Skill, never to creating the Skill
    itself.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Skill name already exists: {name!r}")


class SkillNotFoundError(ValueError):
    """No Skill exists for this id."""

    def __init__(self, skill_id: UUID) -> None:
        self.skill_id = skill_id
        super().__init__(f"Skill not found: {skill_id}")


def _is_skill_name_unique_violation(error: IntegrityError) -> bool:
    """Whether ``error`` was caused specifically by
    :data:`SKILL_NAME_UNIQUE_CONSTRAINT` -- read safely off the underlying
    DBAPI exception's ``constraint_name``, never guessed from the error
    message string (same pattern as
    ``pipeline_submission._is_idempotency_key_unique_violation``)."""

    orig = error.orig
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return constraint_name == SKILL_NAME_UNIQUE_CONSTRAINT


@dataclass(frozen=True, slots=True)
class SkillRevisionOutcome:
    """What a revision-creation call resolved to -- lets a future SM-702/703
    route distinguish a brand-new revision from a safe replay of
    already-existing content, without this layer inventing an HTTP status
    (Feature Contract SS 9.1)."""

    revision: SkillRevision
    created: bool
    """``True``: a brand-new ``SkillRevision`` was just persisted and
    ``Skill.current_revision_id`` now points at it. ``False``: an existing
    revision with the same ``content_sha256`` was found and returned
    unchanged -- ``current_revision_id`` was **not** modified."""


async def create_skill_aggregate(
    uow: PostgresUnitOfWork,
    *,
    name: str,
    content: SkillRevisionContent,
    resolution_embedding: list[float],
) -> Skill:
    """Atomically create a Skill and its first SkillRevision (Feature
    Contract SS 4.4): ``Skill`` + ``SkillRevision revision=1`` +
    ``current_revision_id -> revision 1``, in one transaction.

    Never leaves a committed Skill without a revision, and never leaves a
    partial pair behind on failure: the insert is SAVEPOINT-scoped, so an
    ``IntegrityError`` on ``skills.name`` rolls back only this attempt,
    never the caller's outer transaction (same pattern as
    ``DatasetRepository.get_or_create_by_slug``). The composite,
    ``DEFERRABLE INITIALLY DEFERRED`` foreign key on
    ``Skill.current_revision_id`` is checked at the outer transaction's
    actual commit, by which point both rows exist.

    Raises :class:`SkillNameAlreadyExistsError` if ``name`` already exists
    in any status -- never upserts, never silently returns the existing
    Skill.
    """

    skill_id = uuid4()
    revision_id = uuid4()
    content_sha256 = compute_content_sha256(name=name, content=content)
    now = utc_now()

    skill = Skill(
        id=skill_id,
        name=name,
        status=SkillStatus.ACTIVE,
        current_revision_id=revision_id,
        created_at=now,
        updated_at=now,
    )
    revision = SkillRevision(
        id=revision_id,
        skill_id=skill_id,
        revision=1,
        description=content.description,
        procedure=content.procedure,
        license=content.license,
        compatibility=content.compatibility,
        metadata_=content.metadata,
        tags=content.tags,
        declared_tools=content.declared_tools,
        content_sha256=content_sha256,
        resolution_embedding=resolution_embedding,
        created_at=now,
    )

    try:
        async with uow.savepoint():
            await uow.skills.add(skill)
            await uow.skill_revisions.add(revision)
    except IntegrityError as exc:
        if _is_skill_name_unique_violation(exc):
            raise SkillNameAlreadyExistsError(name) from exc
        raise

    return skill


async def create_skill_revision(
    uow: PostgresUnitOfWork,
    *,
    skill_id: UUID,
    name: str,
    content: SkillRevisionContent,
    resolution_embedding: list[float],
) -> SkillRevisionOutcome:
    """Create a new SkillRevision for an existing Skill, or resolve a safe
    replay of already-existing content (Feature Contract SS 6.3/6.4/9.1).

    Acquires the per-Skill lock itself
    (:meth:`SkillRepository.get_by_id_for_update`), so every
    revision-creating call for the same Skill -- including a future SM-702
    ``current_revision`` rollback issued through the same lock -- is
    serialized against every other one (Feature Contract SS 6.2/6.5): the
    caller must run this inside a transaction on ``uow`` and eventually
    commit it; this function does not commit on its own.

    Raises :class:`SkillNotFoundError` if ``skill_id`` does not exist.
    """

    skill = await uow.skills.get_by_id_for_update(skill_id)
    if skill is None:
        raise SkillNotFoundError(skill_id)

    content_sha256 = compute_content_sha256(name=name, content=content)

    existing = await uow.skill_revisions.get_by_skill_and_content_sha256(skill_id, content_sha256)
    if existing is not None:
        return SkillRevisionOutcome(revision=existing, created=False)

    next_revision = await uow.skill_revisions.next_revision_number(skill_id)
    now = utc_now()
    revision = SkillRevision(
        skill_id=skill_id,
        revision=next_revision,
        description=content.description,
        procedure=content.procedure,
        license=content.license,
        compatibility=content.compatibility,
        metadata_=content.metadata,
        tags=content.tags,
        declared_tools=content.declared_tools,
        content_sha256=content_sha256,
        resolution_embedding=resolution_embedding,
        created_at=now,
    )
    await uow.skill_revisions.add(revision)

    skill.current_revision_id = revision.id
    skill.updated_at = now

    return SkillRevisionOutcome(revision=revision, created=True)


# ---------------------------------------------------------------------------
# SM-702: public management service
# ---------------------------------------------------------------------------


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]


class SkillService:
    """Public Skill management + immutable SkillRevision service backing
    ``api/routes/skills.py``. Owns the embedding boundary: computes
    resolution text and calls the embedding provider itself, entirely
    outside any PostgreSQL transaction, before ever opening a UoW (Feature
    Contract SS 7.1).

    Uses ``PostgresUnitOfWork`` concretely, not a narrow Protocol -- the
    SM-701 persistence primitives (:func:`create_skill_aggregate`,
    :func:`create_skill_revision`) this service calls into are themselves
    concretely typed, so a Protocol layer here would add indirection
    without adding real fake-UoW testability."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def create_skill(self, request: SkillCreateRequest) -> SkillResult:
        content = _content_from_request(request)
        resolution_text = build_skill_resolution_text(
            name=request.name, description=content.description, tags=content.tags
        )
        embedding = await self._resolution_embedding(resolution_text)

        async with self._unit_of_work_factory() as uow:
            try:
                skill = await create_skill_aggregate(
                    uow,
                    name=request.name,
                    content=content,
                    resolution_embedding=embedding,
                )
            except SkillNameAlreadyExistsError as exc:
                raise skill_name_conflict_error(exc.name) from exc
            result = skill_result_from_content(skill, current_revision=1, content=content)
            await uow.commit()
            return result

    async def list_skills(
        self,
        *,
        limit: int,
        offset: int,
        status: SkillStatus | None,
    ) -> SkillListResult:
        async with self._unit_of_work_factory() as uow:
            pairs, total = await uow.skills.list_paginated(
                limit=limit, offset=offset, status=status
            )
            return SkillListResult(
                items=[skill_result_from_revision(skill, revision) for skill, revision in pairs],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_skill(self, skill_uuid: UUID) -> SkillResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill(uow, skill_uuid)
            revision = await require_current_revision(uow, skill)
            return skill_result_from_revision(skill, revision)

    async def update_skill(
        self,
        skill_uuid: UUID,
        request: SkillUpdateRequest,
    ) -> SkillResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill_for_update(uow, skill_uuid)
            target = await uow.skill_revisions.get_by_skill_and_revision(
                skill.id, request.current_revision
            )
            if target is None:
                raise skill_revision_target_missing_error(skill_uuid, request.current_revision)
            if skill.current_revision_id != target.id:
                skill.current_revision_id = target.id
                skill.updated_at = utc_now()
            # else: idempotent no-op -- current_revision already matches, no timestamp churn.
            result = skill_result_from_revision(skill, target)
            await uow.commit()
            return result

    async def archive_skill(self, skill_uuid: UUID) -> SkillResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill_for_update(uow, skill_uuid)
            if skill.status == SkillStatus.ACTIVE:
                now = utc_now()
                skill.status = SkillStatus.ARCHIVED
                skill.archived_at = now
                skill.updated_at = now
            # else: already archived -- idempotent no-op, no timestamp churn.
            # current_revision_id is never touched by archive.
            revision = await require_current_revision(uow, skill)
            result = skill_result_from_revision(skill, revision)
            await uow.commit()
            return result

    async def restore_skill(self, skill_uuid: UUID) -> SkillResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill_for_update(uow, skill_uuid)
            if skill.status == SkillStatus.ARCHIVED:
                skill.status = SkillStatus.ACTIVE
                skill.archived_at = None
                skill.updated_at = utc_now()
            # else: already active -- idempotent no-op, no timestamp churn.
            # current_revision_id is never touched by restore -- it stays at
            # whatever it was set to while archived, never reset to the
            # pointer that existed before the archive.
            revision = await require_current_revision(uow, skill)
            result = skill_result_from_revision(skill, revision)
            await uow.commit()
            return result

    async def create_revision(
        self,
        skill_uuid: UUID,
        request: SkillRevisionCreateRequest,
    ) -> tuple[SkillRevisionResult, bool]:
        # Pre-read the Skill's authoritative name without holding a
        # transaction across the embedding provider call -- the session
        # this UoW owns is closed before _resolution_embedding is ever
        # invoked (Feature Contract SS 7.1). Archive status is irrelevant
        # here: creating a revision while archived is explicitly permitted.
        # `name` is captured as a plain str *inside* the block: closing the
        # UoW expires the ORM instance, so `skill.name` is never read after
        # `__aexit__` runs.
        async with self._unit_of_work_factory() as uow:
            skill = await uow.skills.get_by_id(skill_uuid)
            if skill is None:
                raise skill_not_found_error(skill_uuid)
            name = skill.name

        content = _content_from_request(request)
        resolution_text = build_skill_resolution_text(
            name=name, description=content.description, tags=content.tags
        )
        embedding = await self._resolution_embedding(resolution_text)

        async with self._unit_of_work_factory() as uow:
            try:
                outcome = await create_skill_revision(
                    uow,
                    skill_id=skill_uuid,
                    name=name,
                    content=content,
                    resolution_embedding=embedding,
                )
            except SkillNotFoundError as exc:
                raise skill_not_found_error(skill_uuid) from exc
            result = revision_result(skill_uuid, outcome.revision)
            await uow.commit()
            return result, outcome.created

    async def list_revisions(
        self,
        skill_uuid: UUID,
        *,
        limit: int,
        offset: int,
    ) -> SkillRevisionListResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill(uow, skill_uuid)
            revisions = await uow.skill_revisions.list_for_skill(
                skill.id, limit=limit, offset=offset
            )
            total = await uow.skill_revisions.count_for_skill(skill.id)
            return SkillRevisionListResult(
                items=[revision_list_item(skill.id, revision) for revision in revisions],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_revision(self, skill_uuid: UUID, revision: int) -> SkillRevisionResult:
        async with self._unit_of_work_factory() as uow:
            skill = await require_skill(uow, skill_uuid)
            found = await uow.skill_revisions.get_by_skill_and_revision(skill.id, revision)
            if found is None:
                raise skill_revision_not_found_error(skill_uuid, revision)
            return revision_result(skill.id, found)

    async def _resolution_embedding(self, text: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([text])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        if len(embeddings) != 1 or len(embeddings[0]) != self._settings.embedding_dimensions:
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected vector dimension."
            )
        return embeddings[0]


def _content_from_request(
    request: SkillCreateRequest | SkillRevisionCreateRequest,
) -> SkillRevisionContent:
    return SkillRevisionContent(
        description=request.description,
        procedure=request.procedure,
        license=request.license,
        compatibility=request.compatibility,
        metadata=request.metadata,
        tags=request.tags,
        declared_tools=request.declared_tools,
    )


async def require_skill(uow: PostgresUnitOfWork, skill_uuid: UUID) -> Skill:
    skill = await uow.skills.get_by_id(skill_uuid)
    if skill is None:
        raise skill_not_found_error(skill_uuid)
    return skill


async def require_skill_for_update(uow: PostgresUnitOfWork, skill_uuid: UUID) -> Skill:
    skill = await uow.skills.get_by_id_for_update(skill_uuid)
    if skill is None:
        raise skill_not_found_error(skill_uuid)
    return skill


async def require_current_revision(uow: PostgresUnitOfWork, skill: Skill) -> SkillRevision:
    revision = await uow.skill_revisions.get_by_id(skill.current_revision_id)
    if revision is None:  # pragma: no cover - defensive: DB FK guarantees this can't happen
        raise skill_not_found_error(skill.id)
    return revision


def skill_result_from_revision(skill: Skill, revision: SkillRevision) -> SkillResult:
    return SkillResult(
        skill_uuid=skill.id,
        name=skill.name,
        description=revision.description,
        status=skill.status,
        current_revision=revision.revision,
        compatibility=revision.compatibility,
        tags=revision.tags,
        declared_tools=revision.declared_tools,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        archived_at=skill.archived_at,
    )


def skill_result_from_content(
    skill: Skill,
    *,
    current_revision: int,
    content: SkillRevisionContent,
) -> SkillResult:
    """Build a :class:`SkillResult` right after :func:`create_skill_aggregate`
    from the already-validated request ``content`` -- avoids a redundant
    round-trip read of the SkillRevision this same call just persisted."""

    return SkillResult(
        skill_uuid=skill.id,
        name=skill.name,
        description=content.description,
        status=skill.status,
        current_revision=current_revision,
        compatibility=content.compatibility,
        tags=content.tags,
        declared_tools=content.declared_tools,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        archived_at=skill.archived_at,
    )


def revision_result(skill_uuid: UUID, revision: SkillRevision) -> SkillRevisionResult:
    return SkillRevisionResult(
        skill_uuid=skill_uuid,
        revision=revision.revision,
        description=revision.description,
        procedure=revision.procedure,
        license=revision.license,
        compatibility=revision.compatibility,
        metadata=revision.metadata_,
        tags=revision.tags,
        declared_tools=revision.declared_tools,
        content_sha256=revision.content_sha256,
        created_at=revision.created_at,
    )


def revision_list_item(skill_uuid: UUID, revision: SkillRevision) -> SkillRevisionListItem:
    return SkillRevisionListItem(
        skill_uuid=skill_uuid,
        revision=revision.revision,
        description=revision.description,
        license=revision.license,
        compatibility=revision.compatibility,
        tags=revision.tags,
        declared_tools=revision.declared_tools,
        content_sha256=revision.content_sha256,
        created_at=revision.created_at,
    )


def skill_not_found_error(skill_uuid: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Skill does not exist.",
        details={"skill_uuid": str(skill_uuid)},
    )


def skill_name_conflict_error(name: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.CONFLICT,
        message="Skill with this name already exists.",
        details={"name": name},
    )


def skill_revision_target_missing_error(skill_uuid: UUID, revision: int) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="current_revision does not reference an existing revision of this Skill.",
        details={"skill_uuid": str(skill_uuid), "current_revision": revision},
    )


def skill_revision_not_found_error(skill_uuid: UUID, revision: int) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="SkillRevision does not exist.",
        details={"skill_uuid": str(skill_uuid), "revision": revision},
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work
