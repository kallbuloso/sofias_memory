from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope

router = APIRouter(tags=["info"])

API_CONTRACT_VERSION = "1"
"""Machine-readable API contract version (ADR-0016 SS 16) -- independent of
``version`` (the application release SemVer). A consumer must never infer
Cognitive Memory support from ``version >= 0.7.0`` alone."""

COGNITIVE_MEMORY_CONTRACT_VERSION = "1"

COGNITIVE_MEMORY_CAPABILITIES: tuple[str, ...] = (
    "cognitive_memory.write",
    "cognitive_memory.get",
    "cognitive_memory.recall",
    "cognitive_memory.supersede",
    "cognitive_memory.forget",
)
"""All five v0.7 Cognitive Memory capabilities -- the complete Feature
Contract SS 11 list, in its frozen deterministic order."""

CONTEXTUAL_PROJECT_MEMORY_CONTRACT_VERSION = "1"

CONTEXTUAL_PROJECT_MEMORY_CAPABILITIES: tuple[str, ...] = (
    "contextual_project_memory.episodes",
    "contextual_project_memory.checkpoints",
    "contextual_project_memory.recall",
    "contextual_project_memory.explain",
    "contextual_project_memory.history",
)
"""Exactly the five Contextual Project Memory v1 capabilities frozen by its
Contract (SS 6.1), in their frozen order. Evidence and Forget propagation are
integral guarantees of that contract, never separate capabilities. Each one
corresponds to a working surface: Episodes, Context Checkpoints and the
resume/context, explain and history intents of Contextual Recall."""

CONTRACTS: dict[str, str] = {
    "cognitive_memory": COGNITIVE_MEMORY_CONTRACT_VERSION,
    "contextual_project_memory": CONTEXTUAL_PROJECT_MEMORY_CONTRACT_VERSION,
}
"""Per-feature contract major versions, in deterministic order. Additive:
a feature contract is never removed or renamed implicitly."""

CAPABILITIES: tuple[str, ...] = (
    *COGNITIVE_MEMORY_CAPABILITIES,
    *CONTEXTUAL_PROJECT_MEMORY_CAPABILITIES,
)
"""Every capability string advertised, grouped by feature in a frozen order."""


class ApplicationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Application name.")
    version: str = Field(description="Application release version.")
    environment: str = Field(description="Configured application environment (APP_ENV).")
    config_fingerprint: str = Field(
        description=(
            "Stable hash of the functional configuration currently loaded. Two "
            "instances with the same fingerprint have the same effective settings; "
            "never reveals any secret value itself."
        )
    )
    llm_model: str = Field(description="Configured OpenAI-compatible LLM model.")
    embedding_model: str = Field(description="Configured OpenAI-compatible embedding model.")
    embedding_dimensions: int = Field(description="Configured embedding vector dimensions.")
    api_contract_version: str = Field(
        description=(
            "Machine-readable API contract version -- distinct from `version` "
            "(the application release SemVer), never inferred from it."
        )
    )
    contracts: dict[str, str] = Field(
        description=(
            "Per-feature contract major versions. A consumer negotiates a feature only "
            "through its entry here, never from the application version."
        )
    )
    capabilities: list[str] = Field(
        description=(
            "Capability strings of every advertised feature contract, each one "
            "backed by an implemented, working surface -- never a health/readiness "
            "signal and never an operation that does not exist."
        )
    )


@router.get(
    "/info",
    response_model=SuccessEnvelope[ApplicationInfo],
    summary="Get application info",
    description=(
        "Returns non-secret application identity and configuration -- name, "
        "version, environment, a configuration fingerprint, and the configured "
        "LLM/embedding models. Requires `X-API-Key`."
    ),
)
async def info(request: Request) -> SuccessEnvelope[ApplicationInfo]:
    settings = app_settings(request.app)
    return SuccessEnvelope[ApplicationInfo](
        data=ApplicationInfo(
            name=settings.app_name,
            version=settings.app_version,
            environment=settings.app_env,
            config_fingerprint=settings.config_fingerprint(),
            llm_model=settings.llm_model,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            api_contract_version=API_CONTRACT_VERSION,
            contracts=dict(CONTRACTS),
            capabilities=list(CAPABILITIES),
        ),
        meta=ResponseMeta(request_id=current_request_id()),
    )
