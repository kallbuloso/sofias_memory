"""Create first-class durable Agent Profile persistence foundation (ADR-0014, SM-801).

Purely additive: ``agents`` only. No other table is touched -- Agent is a
global, single-user-instance identity, never Dataset/Session/Skill-owned,
never a runtime/provider-config holder, and never projected to Neo4j or
routed through ``graph_outbox``. ``agent_skills``/``agent_sessions`` are
explicitly out of scope for this migration (SM-803/SM-804).

Agent Profile is mutable in place -- there is no ``AgentRevision`` and
therefore no composite-FK/deferred-constraint ceremony like Skill's
``current_revision_id`` needed: a single ``CREATE TABLE`` is sufficient.

``instructions`` carries two CHECK constraints (max length and nonblank),
mirroring the authoritative-at-the-database-level discipline already used
for ``skill_revisions.procedure``. ``description`` and ``display_name``
carry only a max-length CHECK at the database level -- the Feature Contract
does not order a database-level nonblank/trim constraint for either, and
none is invented here (see ``sofias_memory.domain.agent_profile`` for the
domain-layer rules, including the deliberate asymmetry with ``instructions``).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-09 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_NAME_MAX_LENGTH = 64
AGENT_DISPLAY_NAME_MAX_LENGTH = 120
AGENT_DESCRIPTION_MAX_LENGTH = 1024
AGENT_INSTRUCTIONS_MAX_LENGTH = 65_536

agent_status = postgresql.ENUM(
    "active",
    "archived",
    name="agent_status",
    create_type=False,
)


def upgrade() -> None:
    agent_status.create(op.get_bind())

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            agent_status,
            server_default="active",
            nullable=False,
        ),
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
            f"char_length(name) <= {AGENT_NAME_MAX_LENGTH}",
            name=op.f("ck_agents_name_max_length"),
        ),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_agents_name_portable_charset"),
        ),
        sa.CheckConstraint(
            f"display_name IS NULL OR char_length(display_name) <= {AGENT_DISPLAY_NAME_MAX_LENGTH}",
            name=op.f("ck_agents_display_name_max_length"),
        ),
        sa.CheckConstraint(
            f"description IS NULL OR char_length(description) <= {AGENT_DESCRIPTION_MAX_LENGTH}",
            name=op.f("ck_agents_description_max_length"),
        ),
        sa.CheckConstraint(
            f"instructions IS NULL OR char_length(instructions) <= {AGENT_INSTRUCTIONS_MAX_LENGTH}",
            name=op.f("ck_agents_instructions_max_length"),
        ),
        sa.CheckConstraint(
            r"instructions IS NULL OR instructions ~ '\S'",
            name=op.f("ck_agents_instructions_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("name", name=op.f("uq_agents_name")),
    )


def downgrade() -> None:
    op.drop_table("agents")
    agent_status.drop(op.get_bind())
