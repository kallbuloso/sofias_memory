"""Public Evidence/support schemas (SM-CTX-004, Contract v1 SS 11-13, handoff 12
SS 11/20/21/25/26/33/49).

Request: ``support = {"mode": ..., "refs": [{"source_type", "source_id"}]}``
(``evidence`` is not accepted as a request alias). Result: ``support = {"mode",
"evidence": [{"evidence_id", "source_type", "source_id", "availability"}]}``
in canonical order, or ``null`` when no support is declared. Evidence never
carries source content.

Validation codes: unknown ``source_type`` -> ``INVALID_EVIDENCE_REFERENCE``;
unknown ``mode``, empty ``refs`` or more than
:data:``sofias_memory.domain.MAX_EVIDENCE_REFS_PER_SUPPORT`` refs ->
``INVALID_SUPPORT_POLICY``; duplicate refs -> ``INVALID_REQUEST``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from sofias_memory.domain import (
    MAX_EVIDENCE_REFS_PER_SUPPORT,
    DuplicateEvidenceReferenceError,
    EvidenceAvailability,
    EvidenceKey,
    EvidenceSourceType,
    SupportMode,
    canonical_evidence_keys,
)


def _source_type(value: object) -> EvidenceSourceType:
    if isinstance(value, EvidenceSourceType):
        return value
    if isinstance(value, str):
        try:
            return EvidenceSourceType(value)
        except ValueError:
            pass
    allowed = ", ".join(t.value for t in EvidenceSourceType)
    raise PydanticCustomError(
        "invalid_evidence_reference",
        "{reason}",
        {"reason": f"source_type must be one of: {allowed}"},
    )


def _mode(value: object) -> SupportMode:
    if isinstance(value, SupportMode):
        return value
    if isinstance(value, str):
        try:
            return SupportMode(value)
        except ValueError:
            pass
    allowed = ", ".join(m.value for m in SupportMode)
    raise PydanticCustomError(
        "invalid_support_policy", "{reason}", {"reason": f"mode must be one of: {allowed}"}
    )


EvidenceSourceTypeValue = Annotated[EvidenceSourceType, BeforeValidator(_source_type)]
SupportModeValue = Annotated[SupportMode, BeforeValidator(_mode)]


class EvidenceRefRequest(BaseModel):
    """One explicitly declared Evidence reference (identity only)."""

    model_config = ConfigDict(extra="forbid")

    source_type: EvidenceSourceTypeValue
    source_id: UUID


class SupportRequest(BaseModel):
    """Declared support: deterministic policy + explicit Evidence set."""

    model_config = ConfigDict(extra="forbid")

    mode: SupportModeValue
    refs: list[EvidenceRefRequest] = Field(
        description=(
            f"1..{MAX_EVIDENCE_REFS_PER_SUPPORT} explicit Evidence references; an unordered set "
            "(order never matters), duplicates rejected."
        )
    )

    @model_validator(mode="after")
    def _validate_set(self) -> SupportRequest:
        if not self.refs:
            raise PydanticCustomError(
                "invalid_support_policy",
                "{reason}",
                {
                    "reason": (
                        "support requires at least one Evidence reference; omit support instead"
                    )
                },
            )
        if len(self.refs) > MAX_EVIDENCE_REFS_PER_SUPPORT:
            raise PydanticCustomError(
                "invalid_support_policy",
                "{reason}",
                {"reason": f"support accepts at most {MAX_EVIDENCE_REFS_PER_SUPPORT} references"},
            )
        try:
            canonical_evidence_keys(self.keys())
        except DuplicateEvidenceReferenceError as error:
            raise ValueError(str(error)) from error
        return self

    def keys(self) -> list[EvidenceKey]:
        return [EvidenceKey(ref.source_type, ref.source_id) for ref in self.refs]

    def canonical_keys(self) -> list[EvidenceKey]:
        return canonical_evidence_keys(self.keys())


class EvidenceResult(BaseModel):
    """Evidence identity + current availability (never source content)."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    source_type: EvidenceSourceType
    source_id: UUID
    availability: EvidenceAvailability


class SupportResult(BaseModel):
    """Declared support with Evidence in canonical (source_type, source_id) order."""

    model_config = ConfigDict(extra="forbid")

    mode: SupportMode
    evidence: list[EvidenceResult]
