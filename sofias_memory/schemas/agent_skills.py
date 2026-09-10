"""Public schemas for the Agent <-> Skill association API (SM-803, ADR-0014).

``pinned_revision`` is always the public 1-based monotonic revision integer
(the same one every other Skill revision reference already uses) -- the
internal ``SkillRevision.id`` UUID is never received or returned on any of
these three endpoints.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sofias_memory.domain import SkillStatus


class AgentSkillSetRequest(BaseModel):
    """``PUT /agents/{agent_uuid}/skills/{skill_uuid}`` payload. Omitted is
    equivalent to explicit ``null`` -- both mean "follow the Skill's current
    revision live". A positive integer pins the association to exactly that
    revision of the target Skill."""

    model_config = ConfigDict(extra="forbid")

    pinned_revision: int | None = Field(
        default=None,
        ge=1,
        description="null (or omitted) = follow current; an integer pins that exact revision.",
    )


class AgentSkillResult(BaseModel):
    """One Agent<->Skill association -- management/disclosure shape only.
    Never includes ``procedure``, ``resolution_embedding``, ``metadata``,
    ``license``, ``content_sha256``, or any internal ``SkillRevision.id``."""

    model_config = ConfigDict(extra="forbid")

    skill_uuid: UUID
    name: str
    status: SkillStatus
    current_revision: int
    pinned_revision: int | None
    effective_revision: int
    description: str
    tags: list[str]
    declared_tools: list[str]
    compatibility: str | None
    association_created_at: datetime


class AgentSkillListResult(BaseModel):
    """Full collection of one Agent's associated Skills -- not paginated,
    the Feature Contract did not freeze pagination for this endpoint."""

    model_config = ConfigDict(extra="forbid")

    items: list[AgentSkillResult]
