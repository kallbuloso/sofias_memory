from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope

router = APIRouter(tags=["info"])


class ApplicationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Application name.")
    version: str = Field(description="Application release version.")
    environment: str = Field(description="Configured application environment.")
    config_fingerprint: str = Field(description="Safe functional configuration fingerprint.")
    llm_model: str = Field(description="Configured OpenAI-compatible LLM model.")
    embedding_model: str = Field(description="Configured OpenAI-compatible embedding model.")
    embedding_dimensions: int = Field(description="Configured embedding vector dimensions.")


@router.get("/info", response_model=SuccessEnvelope[ApplicationInfo])
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
        ),
        meta=ResponseMeta(request_id=current_request_id()),
    )
