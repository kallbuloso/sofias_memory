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
"""SM-1004 HEAD: all five v0.7 Cognitive Memory capabilities are
implemented -- the complete Feature Contract SS 11 list, in its frozen
deterministic order. No capability beyond these five exists."""


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
        description="Per-feature contract major versions, e.g. {'cognitive_memory': '1'}."
    )
    capabilities: list[str] = Field(
        description=(
            "Cognitive Memory capability strings actually implemented by this "
            "HEAD -- never a health/readiness signal, never anticipates an "
            "operation whose route/service does not exist yet."
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
            contracts={"cognitive_memory": COGNITIVE_MEMORY_CONTRACT_VERSION},
            capabilities=list(COGNITIVE_MEMORY_CAPABILITIES),
        ),
        meta=ResponseMeta(request_id=current_request_id()),
    )
