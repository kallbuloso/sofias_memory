"""Create first-class durable procedural Skills persistence foundation (ADR-0013, SM-701).

Purely additive: ``skills`` and ``skill_revisions``. No other table is
touched -- Skills are global, never Dataset/Session/Agent-owned, and are
never projected to Neo4j or routed through ``graph_outbox``.

``Skill.current_revision_id`` is enforced NOT NULL from the first committed
row via a composite, ``DEFERRABLE INITIALLY DEFERRED`` foreign key targeting
``skill_revisions``' own ``UNIQUE(skill_id, id)`` candidate key. That
composite FK cannot be declared inline on ``CREATE TABLE skills`` (the
referenced ``skill_revisions`` table does not exist yet), so it is added via
a separate ``ALTER TABLE`` after both tables exist -- see ``upgrade()``.
Because the FK is deferred, a single transaction can insert the ``skills``
row (with ``current_revision_id`` already pointing at a not-yet-inserted
``skill_revisions`` row) and then insert that row, with the composite FK
validated only at commit time, by which point both rows exist. This lets
``current_revision_id`` be NOT NULL from the start, with no window where the
column is temporarily null awaiting a later write, and no committed Skill
ever observable without a revision.

``skill_revisions.metadata`` can never contain the key
``sofias-memory.tags``: that key exists only as a SKILL.md transport
representation of ``tags`` (a future SM-703 import consumes and discards it
before persistence), never as semantic persisted metadata. Enforced by
``ck_skill_revisions_metadata_excludes_reserved_tags_key``.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-07 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SKILL_NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
COMPATIBILITY_MAX_LENGTH = 500
PROCEDURE_MAX_LENGTH = 65_536
CONTENT_SHA256_PATTERN = "'^[0-9a-f]{64}$'"
EMBEDDING_DIMENSIONS = 3072
SOFIAS_MEMORY_TAGS_METADATA_KEY = "sofias-memory.tags"

SKILL_REVISIONS_ANN_INDEX_SQL = (
    "CREATE INDEX ix_skill_revisions_resolution_embedding_halfvec_hnsw "
    "ON skill_revisions USING hnsw "
    "((resolution_embedding::halfvec(3072)) halfvec_cosine_ops)"
)

skill_status = postgresql.ENUM(
    "active",
    "archived",
    name="skill_status",
    create_type=False,
)


def upgrade() -> None:
    skill_status.create(op.get_bind())

    op.create_table(
        "skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            skill_status,
            server_default="active",
            nullable=False,
        ),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"char_length(name) <= {SKILL_NAME_MAX_LENGTH}",
            name=op.f("ck_skills_name_max_length"),
        ),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_skills_name_portable_charset"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint("name", name=op.f("uq_skills_name")),
    )

    op.create_table(
        "skill_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("procedure", sa.Text(), nullable=False),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("compatibility", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "declared_tools",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("resolution_embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_skill_revisions_skill_id_skills"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_skill_revisions_revision_positive")),
        sa.CheckConstraint(
            f"char_length(description) BETWEEN 1 AND {DESCRIPTION_MAX_LENGTH}",
            name=op.f("ck_skill_revisions_description_length"),
        ),
        sa.CheckConstraint(
            f"char_length(procedure) <= {PROCEDURE_MAX_LENGTH}",
            name=op.f("ck_skill_revisions_procedure_max_length"),
        ),
        sa.CheckConstraint(
            r"procedure ~ '\S'",
            name=op.f("ck_skill_revisions_procedure_not_blank"),
        ),
        sa.CheckConstraint(
            "compatibility IS NULL OR char_length(compatibility) BETWEEN 1 AND "
            f"{COMPATIBILITY_MAX_LENGTH}",
            name=op.f("ck_skill_revisions_compatibility_length"),
        ),
        sa.CheckConstraint(
            f"content_sha256 ~ {CONTENT_SHA256_PATTERN}",
            name=op.f("ck_skill_revisions_content_sha256_hex"),
        ),
        sa.CheckConstraint(
            f"NOT (metadata ? '{SOFIAS_MEMORY_TAGS_METADATA_KEY}')",
            name=op.f("ck_skill_revisions_metadata_excludes_reserved_tags_key"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_revisions")),
        sa.UniqueConstraint(
            "skill_id",
            "revision",
            name="uq_skill_revisions_skill_id_revision",
        ),
        sa.UniqueConstraint(
            "skill_id",
            "id",
            name="uq_skill_revisions_skill_id_id",
        ),
        sa.UniqueConstraint(
            "skill_id",
            "content_sha256",
            name="uq_skill_revisions_skill_id_content_sha256",
        ),
    )
    op.execute(SKILL_REVISIONS_ANN_INDEX_SQL)

    # Composite, deferred FK enforcing that a Skill's current revision
    # always belongs to that exact Skill (see module docstring). Added after
    # both tables exist, since skill_revisions.(skill_id, id) is the
    # candidate key it targets.
    op.create_foreign_key(
        op.f("fk_skills_current_revision_skill_revisions"),
        "skills",
        "skill_revisions",
        ["id", "current_revision_id"],
        ["skill_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_skills_current_revision_skill_revisions"),
        "skills",
        type_="foreignkey",
    )
    op.execute("DROP INDEX ix_skill_revisions_resolution_embedding_halfvec_hnsw")
    op.drop_table("skill_revisions")
    op.drop_table("skills")

    skill_status.drop(op.get_bind())
