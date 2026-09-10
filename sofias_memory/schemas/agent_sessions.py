"""Public schemas for the Agent <-> Session association API (SM-804, ADR-0014).

No request schema exists for ``PUT`` -- the association carries no payload
at all (no ``role``, no ``metadata``, no provenance field of any kind).
``AgentSessionResult`` is a current explicit management association read,
never a transcript, provenance record, or historical participation log.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sofias_memory.domain import SessionStatus


class AgentSessionResult(BaseModel):
    """One Agent<->Session association -- management/disclosure shape only.
    Never includes Session content/context (``metadata``, ``SessionEntry``,
    ``Query``, ``PipelineRun``, or any Session timestamp other than this
    association's own ``association_created_at``)."""

    model_config = ConfigDict(extra="forbid")

    session_uuid: UUID
    session_id: str
    name: str | None
    status: SessionStatus
    association_created_at: datetime


class AgentSessionListResult(BaseModel):
    """Full collection of one Agent's associated Sessions -- not paginated,
    the Feature Contract did not freeze pagination for this endpoint.
    Includes archived Sessions."""

    model_config = ConfigDict(extra="forbid")

    items: list[AgentSessionResult]
