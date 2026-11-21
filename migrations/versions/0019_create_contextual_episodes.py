"""Create first-class contextual Episodes (SM-CTX-001, ADR-0017, Contract v1
Contextual Project Memory SS 14-22/36/67).

Purely additive: ``episodes``, ``episode_provenance``, and the shared
``contextual_memory_idempotency`` ledger, plus the ``episode_kind``,
``episode_lifecycle`` and ``contextual_memory_operation`` enums. No existing
table is touched; no ``projects``/``context_streams`` table, no Neo4j
projection, no ``graph_outbox`` event type, no PipelineRun/PipelineStep row.

``episode_provenance.origin_kind`` reuses the existing
``cognitive_memory_origin_kind`` enum (same five approved values).

``contextual_memory_operation`` already carries the approved ``checkpoint_*``
values as schema readiness only (handoff 09 SS 24) -- no Checkpoint behaviour
exists in this revision.

``contextual_memory_idempotency`` is resource-generic: ``target_resource_id``/
``result_resource_id`` deliberately have no foreign key (the resource type is
determined by ``operation``), and ``idempotency_key`` stores only a protected
64-hex HMAC lookup identity -- never the raw header.

``episodes.embedding`` is the authoritative full-precision ``VECTOR(3072)``,
nullable only because FORGOTTEN must physically lose it. No ANN index:
retrieval is exact cosine. See
``sofias_memory/infrastructure/postgres/models/episode.py`` for the rationale
of each lifecycle CHECK constraint, mirrored here.

Downgrade drops exactly what this revision created; it destroys any Episode
data, which is inherent to reverting this additive revision.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-20 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 3072
HEX_DIGEST_PATTERN = "'^[0-9a-f]{64}$'"

episode_kind = postgresql.ENUM(
    "decision",
    "investigation",
    "experiment",
    "incident",
    "discovery",
    "change",
    "outcome",
    "learning",
    "milestone",
    name="episode_kind",
    create_type=False,
)
episode_lifecycle = postgresql.ENUM(
    "active",
    "invalidated",
    "forgotten",
    name="episode_lifecycle",
    create_type=False,
)
contextual_memory_operation = postgresql.ENUM(
    "episode_create",
    "episode_invalidate",
    "episode_forget",
    "checkpoint_create",
    "checkpoint_supersede",
    "checkpoint_invalidate",
    "checkpoint_forget",
    name="contextual_memory_operation",
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
"""Existing enum created by 0018 -- referenced, never created/dropped here."""


def upgrade() -> None:
    bind = op.get_bind()
    episode_kind.create(bind)
    episode_lifecycle.create(bind)
    contextual_memory_operation.create(bind)

    op.create_table(
        "episodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("context_key", sa.Text(), nullable=True),
        sa.Column("kind", episode_kind, nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("situation", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("lifecycle", episode_lifecycle, server_default="active", nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("replacement_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["replacement_episode_id"],
            ["episodes.id"],
            name=op.f("fk_episodes_replacement_episode_id_episodes"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name=op.f("ck_episodes_confidence_bounds"),
        ),
        sa.CheckConstraint(
            "scope IS NULL OR scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'",
            name=op.f("ck_episodes_scope_grammar"),
        ),
        sa.CheckConstraint(
            "context_key IS NULL OR context_key ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'",
            name=op.f("ck_episodes_context_key_grammar"),
        ),
        sa.CheckConstraint(
            r"summary IS NULL OR summary ~ '\S'", name=op.f("ck_episodes_summary_not_blank")
        ),
        sa.CheckConstraint(
            r"situation IS NULL OR situation ~ '\S'",
            name=op.f("ck_episodes_situation_not_blank"),
        ),
        sa.CheckConstraint(
            r"reason IS NULL OR reason ~ '\S'", name=op.f("ck_episodes_reason_not_blank")
        ),
        sa.CheckConstraint(
            r"outcome IS NULL OR outcome ~ '\S'", name=op.f("ck_episodes_outcome_not_blank")
        ),
        sa.CheckConstraint(
            r"invalidation_reason IS NULL OR invalidation_reason ~ '\S'",
            name=op.f("ck_episodes_invalidation_reason_not_blank"),
        ),
        sa.CheckConstraint(
            "replacement_episode_id IS NULL OR replacement_episode_id <> id",
            name=op.f("ck_episodes_no_self_replacement"),
        ),
        sa.CheckConstraint(
            "lifecycle = 'forgotten' "
            "OR (scope IS NOT NULL AND kind IS NOT NULL AND summary IS NOT NULL "
            "AND occurred_at IS NOT NULL AND embedding IS NOT NULL)",
            name=op.f("ck_episodes_non_forgotten_requires_payload"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'active' "
            "OR (invalidated_at IS NULL AND invalidation_reason IS NULL "
            "AND replacement_episode_id IS NULL AND forgotten_at IS NULL)",
            name=op.f("ck_episodes_active_requires_clean_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'invalidated' "
            "OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name=op.f("ck_episodes_invalidated_requires_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND scope IS NULL AND context_key IS NULL "
            "AND kind IS NULL AND summary IS NULL AND situation IS NULL AND reason IS NULL "
            "AND outcome IS NULL AND occurred_at IS NULL AND embedding IS NULL "
            "AND confidence IS NULL AND invalidation_reason IS NULL "
            "AND replacement_episode_id IS NULL)",
            name=op.f("ck_episodes_forgotten_requires_tombstone_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
    )
    op.create_index(
        "ix_episodes_scope_lifecycle_occurred_at",
        "episodes",
        ["scope", "lifecycle", sa.text("occurred_at DESC"), "id"],
    )
    op.create_index(
        "ix_episodes_scope_context_key_lifecycle_occurred_at",
        "episodes",
        ["scope", "context_key", "lifecycle", sa.text("occurred_at DESC"), "id"],
    )
    op.create_index("ix_episodes_kind", "episodes", ["kind"])
    op.create_index("ix_episodes_occurred_at", "episodes", ["occurred_at"])

    op.create_table(
        "episode_provenance",
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_kind", cognitive_memory_origin_kind, nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("conversation_ref", sa.Text(), nullable=True),
        sa.Column("turn_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_episode_provenance_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            r"source_system ~ '\S'", name=op.f("ck_episode_provenance_source_system_not_blank")
        ),
        sa.CheckConstraint(
            r"conversation_ref IS NULL OR conversation_ref ~ '\S'",
            name=op.f("ck_episode_provenance_conversation_ref_not_blank"),
        ),
        sa.CheckConstraint(
            r"turn_ref IS NULL OR turn_ref ~ '\S'",
            name=op.f("ck_episode_provenance_turn_ref_not_blank"),
        ),
        sa.PrimaryKeyConstraint("episode_id", name=op.f("pk_episode_provenance")),
    )

    op.create_table(
        "contextual_memory_idempotency",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.CHAR(64), nullable=False),
        sa.Column("operation", contextual_memory_operation, nullable=False),
        sa.Column("request_digest", sa.CHAR(64), nullable=False),
        sa.Column("target_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"idempotency_key ~ {HEX_DIGEST_PATTERN}",
            name=op.f("ck_contextual_memory_idempotency_idempotency_key_hex"),
        ),
        sa.CheckConstraint(
            f"request_digest ~ {HEX_DIGEST_PATTERN}",
            name=op.f("ck_contextual_memory_idempotency_request_digest_hex"),
        ),
        sa.CheckConstraint(
            "(operation IN ('episode_create', 'checkpoint_create') "
            "AND target_resource_id IS NULL) "
            "OR (operation NOT IN ('episode_create', 'checkpoint_create') "
            "AND target_resource_id IS NOT NULL)",
            name=op.f("ck_contextual_memory_idempotency_operation_target_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contextual_memory_idempotency")),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_contextual_memory_idempotency_idempotency_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("contextual_memory_idempotency")
    op.drop_table("episode_provenance")
    op.drop_index("ix_episodes_occurred_at", table_name="episodes")
    op.drop_index("ix_episodes_kind", table_name="episodes")
    op.drop_index("ix_episodes_scope_context_key_lifecycle_occurred_at", table_name="episodes")
    op.drop_index("ix_episodes_scope_lifecycle_occurred_at", table_name="episodes")
    op.drop_table("episodes")

    bind = op.get_bind()
    contextual_memory_operation.drop(bind)
    episode_lifecycle.drop(bind)
    episode_kind.drop(bind)
