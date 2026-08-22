"""PipelineContext (ADR-0009 SS O).

Carries only handles and operational state a step actually needs: run
identity, the top-level request input, already-succeeded prior steps'
outputs (reconstructed from PostgreSQL, never trusted from in-process
memory alone -- ADR-0009 SS 13), and a session factory for a step that must
persist its own authoritative PostgreSQL mutation. It is not a service
locator: ``resources`` is a plain, explicitly-populated mapping the engine's
caller supplies (e.g. LLM/embedding clients in SM-510..513); nothing here
resolves a dependency dynamically by string key on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sofias_memory.domain import PipelineType
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory


@dataclass(frozen=True, slots=True)
class PipelineContext:
    run_id: UUID
    pipeline_type: PipelineType
    dataset_id: UUID | None
    source_id: UUID | None
    run_input: Mapping[str, Any]
    step_outputs: Mapping[str, Mapping[str, Any]]
    session_factory: AsyncSessionFactory
    resources: Mapping[str, Any] = field(default_factory=dict)
