"""Skill/SkillRevision persistence primitives (ADR-0013, SM-701).

Pure persistence primitives, reusable by the future SM-702 (Skill
management + revision API) and SM-703 (SKILL.md import/export) routes. No
FastAPI/HTTP-status/ErrorCode concerns live here -- callers translate the
plain exceptions below into the public error contract when the route layer
is built; this module only decides *created vs. replayed vs. conflict*, per
Feature Contract SS 9.1/17.

Embedding boundary (Feature Contract SS 7.1): every function here accepts an
already-computed ``resolution_embedding``. Nothing in this module calls an
embedding provider -- that HTTP call happens in the caller, entirely outside
any PostgreSQL transaction, before these functions are ever invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.domain import SkillRevisionContent, SkillStatus, compute_content_sha256
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import utc_now

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

    skill = Skill(
        id=skill_id,
        name=name,
        status=SkillStatus.ACTIVE,
        current_revision_id=revision_id,
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
    )
    await uow.skill_revisions.add(revision)

    skill.current_revision_id = revision.id
    skill.updated_at = utc_now()

    return SkillRevisionOutcome(revision=revision, created=True)
