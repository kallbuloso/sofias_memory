"""Create the Native Cognitive Memory foundation (ADR-0016, Feature Contract
v0.7.0 Native Cognitive Memory, SM-1001).

Purely additive: ``memory_items``, ``memory_provenance``, and
``cognitive_memory_idempotency``. No other table is touched -- Cognitive
Memory is never represented as Dataset/Source/Document/Chunk/SessionEntry/
Skill/Agent/Query/PipelineRun, and this revision creates no Neo4j
projection, no ``graph_outbox`` event type, and no PipelineRun/PipelineStep
row.

``memory_items.embedding`` is the authoritative full-precision
``VECTOR(3072)`` (ADR-0006); it is nullable only because FORGOTTEN must
physically lose its vector. No ANN/HNSW index is created here -- v0.7 typed
recall (SM-1003) uses exact cosine over the authoritative vector.

Lifecycle invariants (ACTIVE/SUPERSEDED/FORGOTTEN cognitive-payload shape,
self-supersession, single-direct-predecessor lineage) are enforced by
CHECK/partial-UNIQUE constraints so they are structurally testable in
PostgreSQL, not merely conventions -- see
``sofias_memory/infrastructure/postgres/models/memory_item.py`` for the
authoritative rationale of each constraint, mirrored here.

``memory_provenance`` deliberately carries no CHECK constraint enforcing
"origin X requires ref Y": those rules (Feature Contract SS 7.3) apply only
to a non-FORGOTTEN item and are application-enforced, because a DB-level
CHECK of that shape would make the Forget-approved scrub (preserving only
``origin_kind``/``source_system`` with every external ref set to ``NULL``)
structurally impossible.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-13 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 3072
COGNITIVE_MEMORY_CONTENT_MAX_LENGTH = 16384
COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH = 64
COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH = 255
REQUEST_DIGEST_HEX_PATTERN = "'^[0-9a-f]{64}$'"

cognitive_memory_type = postgresql.ENUM(
    "profile",
    "semantic",
    name="cognitive_memory_type",
    create_type=False,
)
cognitive_memory_lifecycle = postgresql.ENUM(
    "active",
    "superseded",
    "forgotten",
    name="cognitive_memory_lifecycle",
    create_type=False,
)
cognitive_memory_origin_kind = postgresql.ENUM(
    "user_asserted",
    "tool_observed",
    "imported",
    "inferred",
    "assistant_generated",
    name="cognitive_memory_origin_kind",
    create_type=False,
)
cognitive_memory_operation = postgresql.ENUM(
    "create",
    "supersede",
    "forget",
    name="cognitive_memory_operation",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    cognitive_memory_type.create(bind)
    cognitive_memory_lifecycle.create(bind)
    cognitive_memory_origin_kind.create(bind)
    cognitive_memory_operation.create(bind)

    op.create_table(
        "memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_type", cognitive_memory_type, nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "lifecycle",
            cognitive_memory_lifecycle,
            server_default="active",
            nullable=False,
        ),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["memory_items.id"],
            name=op.f("fk_memory_items_superseded_by_memory_items"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name=op.f("ck_memory_items_confidence_bounds"),
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name=op.f("ck_memory_items_validity_window_ordered"),
        ),
        sa.CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id",
            name=op.f("ck_memory_items_no_self_supersession"),
        ),
        sa.CheckConstraint(
            r"content IS NULL OR content ~ '\S'",
            name=op.f("ck_memory_items_content_not_blank"),
        ),
        sa.CheckConstraint(
            f"content IS NULL OR char_length(content) <= {COGNITIVE_MEMORY_CONTENT_MAX_LENGTH}",
            name=op.f("ck_memory_items_content_max_length"),
        ),
        sa.CheckConstraint(
            "scope IS NULL OR scope = 'global' OR scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'",
            name=op.f("ck_memory_items_scope_grammar"),
        ),
        sa.CheckConstraint(
            "lifecycle = 'forgotten' "
            "OR (scope IS NOT NULL AND content IS NOT NULL AND embedding IS NOT NULL)",
            name=op.f("ck_memory_items_non_forgotten_requires_cognitive_payload"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'active' "
            "OR (superseded_at IS NULL AND superseded_by IS NULL AND forgotten_at IS NULL)",
            name=op.f("ck_memory_items_active_requires_clean_lineage"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'superseded' "
            "OR (superseded_at IS NOT NULL AND superseded_by IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name=op.f("ck_memory_items_superseded_requires_lineage_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND scope IS NULL AND content IS NULL "
            "AND embedding IS NULL AND confidence IS NULL AND valid_from IS NULL "
            "AND valid_until IS NULL)",
            name=op.f("ck_memory_items_forgotten_requires_tombstone_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(
        "uq_memory_items_superseded_by",
        "memory_items",
        ["superseded_by"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NOT NULL"),
    )

    op.create_table(
        "memory_provenance",
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_kind", cognitive_memory_origin_kind, nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("conversation_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("turn_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmation_ref", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memory_items.id"],
            name=op.f("fk_memory_provenance_memory_id_memory_items"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "source_system ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(source_system) <= "
            f"{COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH}",
            name=op.f("ck_memory_provenance_source_system_slug"),
        ),
        sa.CheckConstraint(
            "confirmation_ref IS NULL OR char_length(confirmation_ref) BETWEEN 1 AND "
            f"{COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH}",
            name=op.f("ck_memory_provenance_confirmation_ref_length"),
        ),
        sa.CheckConstraint(
            "source_ref IS NULL OR char_length(source_ref) BETWEEN 1 AND "
            f"{COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH}",
            name=op.f("ck_memory_provenance_source_ref_length"),
        ),
        sa.PrimaryKeyConstraint("memory_id", name=op.f("pk_memory_provenance")),
    )

    op.create_table(
        "cognitive_memory_idempotency",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("operation", cognitive_memory_operation, nullable=False),
        sa.Column("request_digest", sa.CHAR(64), nullable=False),
        sa.Column("target_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["target_memory_id"],
            ["memory_items.id"],
            name=op.f("fk_cognitive_memory_idempotency_target_memory_id_memory_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_memory_id"],
            ["memory_items.id"],
            name=op.f("fk_cognitive_memory_idempotency_result_memory_id_memory_items"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"request_digest ~ {REQUEST_DIGEST_HEX_PATTERN}",
            name=op.f("ck_cognitive_memory_idempotency_request_digest_hex"),
        ),
        sa.CheckConstraint(
            "(operation = 'create' AND target_memory_id IS NULL) "
            "OR (operation IN ('supersede', 'forget') AND target_memory_id IS NOT NULL)",
            # Kept short deliberately -- see memory_item's ORM model sibling
            # module docstring: the natural long name exceeds PostgreSQL's
            # 63-byte identifier limit here and gets silently truncated.
            name=op.f("ck_cognitive_memory_idempotency_operation_target_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cognitive_memory_idempotency")),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_cognitive_memory_idempotency_idempotency_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("cognitive_memory_idempotency")
    op.drop_table("memory_provenance")
    op.execute("DROP INDEX uq_memory_items_superseded_by")
    op.drop_table("memory_items")

    bind = op.get_bind()
    cognitive_memory_operation.drop(bind)
    cognitive_memory_origin_kind.drop(bind)
    cognitive_memory_lifecycle.drop(bind)
    cognitive_memory_type.drop(bind)
