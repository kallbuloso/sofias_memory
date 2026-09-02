from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlparse, urlunparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

API_KEY_PREFIX = "sf-"
API_KEY_MIN_RANDOM_CHARACTERS = 32
API_KEY_PATTERN = re.compile(rf"^{API_KEY_PREFIX}[A-Za-z0-9_-]{{32,}}$")
EXPECTED_EMBEDDING_DIMENSIONS = 3072
FINGERPRINT_SCHEMA_VERSION = 1
DEFAULT_PROMPT_VERSIONS: Mapping[str, str] = {
    "dataset_summary": "v1",
    "document_summary": "v1",
    "graph_extraction": "v1",
}
ConfigFingerprintPayload = dict[str, object]


def _load_canonical_app_version() -> str:
    """Read the project's canonical version from ``pyproject.toml``.

    ``pyproject.toml`` is not installed as package metadata (there is no
    ``[build-system]`` in this project, and the release image runs
    ``uv sync --no-install-project``), so ``importlib.metadata.version()``
    cannot resolve it. Instead this reads the same file directly from a
    location relative to this module -- one directory above the
    ``sofias_memory`` package -- which resolves identically in a source
    checkout and inside the release image (the Dockerfile copies
    ``pyproject.toml`` next to the ``sofias_memory`` package, at that same
    relative position), with no dependency on process working directory.
    """

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


CANONICAL_APP_VERSION = _load_canonical_app_version()
"""The project's single source of truth for its own version, read once at
import time directly from ``pyproject.toml``. ``Settings.app_version``
defaults to this value; set ``APP_VERSION`` explicitly only to intentionally
report different deployment metadata than the code actually running (e.g. a
CI-injected build identifier) -- it is not a second place to declare the
project's version."""


def _secret_is_blank(value: SecretStr) -> bool:
    return not value.get_secret_value() or value.get_secret_value().isspace()


def normalize_storage_s3_prefix(value: str) -> str:
    """ADR-0011 D6: centralized ``STORAGE_S3_PREFIX`` normalization -- no
    leading/trailing slash, no empty/``.``/``..``/backslash path segment.
    Reused verbatim as a literal key-path segment by the S3 adapter, so an
    ambiguous or unsafe prefix must fail closed here rather than let the
    adapter silently construct an inconsistent key."""

    stripped = value.strip("/")
    if not stripped:
        return ""
    for segment in stripped.split("/"):
        if not segment or segment in {".", ".."} or "\\" in segment:
            raise ValueError(
                "STORAGE_S3_PREFIX must not contain empty, '.', '..', or backslash segments"
            )
    return stripped


def _raw_secret_is_absent(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == "" or value.isspace()
    if isinstance(value, SecretStr):
        secret = value.get_secret_value()
        return secret == "" or secret.isspace()
    return False


class Settings(BaseSettings):
    """Typed process configuration loaded from environment or a UTF-8 .env file.

    Unknown OS environment variables are ignored by pydantic-settings because only
    declared fields are read. Unknown keys in a project .env file are forbidden so
    typos in Sofias Memory settings fail early instead of being silently skipped.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        case_sensitive=True,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    app_name: str = Field(default="Sofias Memory", alias="APP_NAME")
    app_env: str = Field(default="production", alias="APP_ENV")
    app_version: str = Field(default=CANONICAL_APP_VERSION, alias="APP_VERSION")
    api_key: SecretStr = Field(alias="API_KEY")
    http_host: str = Field(default="0.0.0.0", alias="HTTP_HOST")
    http_port: int = Field(default=8000, gt=0, le=65535, alias="HTTP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        alias="CORS_ALLOWED_ORIGINS",
    )
    max_request_body_mb: int = Field(default=50, gt=0, alias="MAX_REQUEST_BODY_MB")
    request_wait_timeout_seconds: int = Field(
        default=30,
        gt=0,
        alias="REQUEST_WAIT_TIMEOUT_SECONDS",
    )

    database_url: SecretStr = Field(alias="DATABASE_URL")
    database_pool_size: int = Field(default=10, gt=0, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=10, ge=0, alias="DATABASE_MAX_OVERFLOW")

    neo4j_uri: str = Field(default="bolt://neo4j:7687", alias="NEO4J_URI")
    neo4j_username: str = Field(default="neo4j", alias="NEO4J_USERNAME")
    neo4j_password: SecretStr = Field(alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")

    data_directory: Path = Field(default=Path("/data/sources"), alias="DATA_DIRECTORY")
    temp_directory: Path = Field(default=Path("/data/tmp"), alias="TEMP_DIRECTORY")
    max_source_size_mb: int = Field(default=50, gt=0, alias="MAX_SOURCE_SIZE_MB")
    # ADR-0011 D2: closed two-value backend selector for where NEW finalized
    # Source originals are written. "s3" is accepted here (the closed enum is
    # frozen by the accepted ADR) but has no adapter yet -- SourceStorageRouter
    # fails closed with a clear configuration error until STORAGE-002 lands.
    storage_backend: Literal["filesystem", "s3"] = Field(
        default="filesystem", alias="STORAGE_BACKEND"
    )
    # ADR-0011 D16: mandatory only when storage_backend == "s3" (validated
    # below); filesystem startup never requires any of these. Credentials are
    # optional so the standard AWS provider chain (env/shared config/IMDS/
    # instance role) remains usable without forcing static secrets.
    storage_s3_bucket: str | None = Field(default=None, alias="STORAGE_S3_BUCKET")
    storage_s3_prefix: str = Field(default="", alias="STORAGE_S3_PREFIX")
    storage_s3_region: str | None = Field(default=None, alias="STORAGE_S3_REGION")
    storage_s3_endpoint_url: str | None = Field(default=None, alias="STORAGE_S3_ENDPOINT_URL")
    storage_s3_access_key_id: SecretStr | None = Field(
        default=None, alias="STORAGE_S3_ACCESS_KEY_ID"
    )
    storage_s3_secret_access_key: SecretStr | None = Field(
        default=None, alias="STORAGE_S3_SECRET_ACCESS_KEY"
    )
    storage_s3_session_token: SecretStr | None = Field(
        default=None, alias="STORAGE_S3_SESSION_TOKEN"
    )
    storage_s3_max_concurrency: int = Field(default=4, ge=1, alias="STORAGE_S3_MAX_CONCURRENCY")

    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_api_key: SecretStr = Field(alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-5-mini", alias="LLM_MODEL")
    llm_timeout_seconds: int = Field(default=120, gt=0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, ge=0, alias="LLM_MAX_RETRIES")
    llm_max_concurrency: int = Field(default=4, ge=1, alias="LLM_MAX_CONCURRENCY")

    embedding_base_url: str = Field(
        default="https://api.openai.com/v1",
        alias="EMBEDDING_BASE_URL",
    )
    embedding_api_key: SecretStr | None = Field(default=None, alias="EMBEDDING_API_KEY")
    embedding_model: str = Field(default="text-embedding-3-large", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(
        default=EXPECTED_EMBEDDING_DIMENSIONS,
        gt=0,
        alias="EMBEDDING_DIMENSIONS",
    )
    embedding_batch_size: int = Field(default=64, gt=0, alias="EMBEDDING_BATCH_SIZE")
    embedding_max_concurrency: int = Field(default=4, ge=1, alias="EMBEDDING_MAX_CONCURRENCY")

    chunk_max_tokens: int = Field(default=900, gt=0, alias="CHUNK_MAX_TOKENS")
    chunk_overlap_tokens: int = Field(default=120, ge=0, alias="CHUNK_OVERLAP_TOKENS")
    chunk_min_tokens: int = Field(default=40, gt=0, alias="CHUNK_MIN_TOKENS")

    recall_vector_top_k: int = Field(default=50, gt=0, alias="RECALL_VECTOR_TOP_K")
    recall_lexical_top_k: int = Field(default=50, gt=0, alias="RECALL_LEXICAL_TOP_K")
    recall_graph_seed_top_k: int = Field(default=10, gt=0, alias="RECALL_GRAPH_SEED_TOP_K")
    recall_graph_depth: int = Field(default=2, gt=0, alias="RECALL_GRAPH_DEPTH")
    recall_graph_max_nodes: int = Field(default=100, gt=0, alias="RECALL_GRAPH_MAX_NODES")
    recall_default_top_k: int = Field(default=12, gt=0, alias="RECALL_DEFAULT_TOP_K")
    recall_max_top_k: int = Field(default=100, gt=0, alias="RECALL_MAX_TOP_K")
    recall_rrf_k: int = Field(default=60, gt=0, alias="RECALL_RRF_K")

    graph_subgraph_max_depth: int = Field(default=3, gt=0, alias="GRAPH_SUBGRAPH_MAX_DEPTH")
    graph_subgraph_max_relations: int = Field(
        default=200, gt=0, alias="GRAPH_SUBGRAPH_MAX_RELATIONS"
    )
    graph_path_max_depth: int = Field(default=4, gt=0, alias="GRAPH_PATH_MAX_DEPTH")
    provenance_max_evidence: int = Field(default=50, gt=0, alias="PROVENANCE_MAX_EVIDENCE")

    entity_dedup_similarity_threshold: float = Field(
        default=0.90,
        gt=0.0,
        le=1.0,
        alias="ENTITY_DEDUP_SIMILARITY_THRESHOLD",
    )
    entity_merge_similarity_threshold: float = Field(
        default=0.95,
        gt=0.0,
        le=1.0,
        alias="ENTITY_MERGE_SIMILARITY_THRESHOLD",
    )

    worker_enabled: bool = Field(default=True, alias="WORKER_ENABLED")
    worker_poll_interval_ms: int = Field(default=500, gt=0, alias="WORKER_POLL_INTERVAL_MS")
    worker_stale_after_seconds: int = Field(default=300, gt=0, alias="WORKER_STALE_AFTER_SECONDS")
    worker_max_concurrent_datasets: int = Field(
        default=1,
        ge=1,
        alias="WORKER_MAX_CONCURRENT_DATASETS",
    )
    worker_max_concurrent_reads: int = Field(default=8, ge=1, alias="WORKER_MAX_CONCURRENT_READS")

    store_query_content: bool = Field(default=True, alias="STORE_QUERY_CONTENT")
    log_document_content: bool = Field(default=False, alias="LOG_DOCUMENT_CONTENT")
    log_llm_payloads: bool = Field(default=False, alias="LOG_LLM_PAYLOADS")

    def config_fingerprint(self, prompt_versions: Mapping[str, str] | None = None) -> str:
        return build_config_fingerprint(self, prompt_versions=prompt_versions)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        random_part = secret.removeprefix(API_KEY_PREFIX)
        if not secret.startswith(API_KEY_PREFIX):
            raise ValueError("API_KEY must start with sf-")
        if not API_KEY_PATTERN.fullmatch(secret):
            raise ValueError("API_KEY must contain only URL-safe characters after sf-")
        if len(random_part) < API_KEY_MIN_RANDOM_CHARACTERS:
            raise ValueError("API_KEY must include at least 32 random characters after sf-")
        if "change-me" in secret.lower():
            raise ValueError("API_KEY placeholder is not allowed")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if _secret_is_blank(value):
            raise ValueError("DATABASE_URL is required")
        parsed = urlparse(value.get_secret_value())
        if parsed.scheme != "postgresql+asyncpg" or not parsed.netloc:
            raise ValueError("DATABASE_URL must be a postgresql+asyncpg URL")
        return value

    @field_validator("neo4j_uri")
    @classmethod
    def validate_neo4j_uri(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"bolt", "neo4j"} or not parsed.netloc:
            raise ValueError("NEO4J_URI must be a bolt:// or neo4j:// URL")
        return value

    @field_validator("llm_base_url", "embedding_base_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must be an absolute http(s) URL")
        return value

    @field_validator("storage_s3_endpoint_url")
    @classmethod
    def validate_storage_s3_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("STORAGE_S3_ENDPOINT_URL must be an absolute http(s) URL")
        return value

    @field_validator("storage_s3_prefix")
    @classmethod
    def validate_storage_s3_prefix(cls, value: str) -> str:
        return normalize_storage_s3_prefix(value)

    @field_validator("llm_api_key", "neo4j_password")
    @classmethod
    def validate_required_secret(cls, value: SecretStr) -> SecretStr:
        if _secret_is_blank(value):
            raise ValueError("secret value is required")
        return value

    @field_validator("embedding_api_key", mode="before")
    @classmethod
    def normalize_empty_embedding_key(cls, value: object) -> object:
        if _raw_secret_is_absent(value):
            return None
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value: object) -> tuple[str, ...] | object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        if isinstance(value, list | tuple):
            return tuple(str(origin).strip() for origin in value if str(origin).strip())
        return value

    @model_validator(mode="before")
    @classmethod
    def inherit_embedding_api_key(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        values = data.copy()
        embedding_key = values.get("EMBEDDING_API_KEY", values.get("embedding_api_key"))
        if not _raw_secret_is_absent(embedding_key):
            return values

        if "LLM_API_KEY" in values:
            values["EMBEDDING_API_KEY"] = values["LLM_API_KEY"]
        elif "llm_api_key" in values:
            values["embedding_api_key"] = values["llm_api_key"]

        return values

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> Self:
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be less than CHUNK_MAX_TOKENS")
        if self.chunk_min_tokens > self.chunk_max_tokens:
            raise ValueError("CHUNK_MIN_TOKENS must be less than or equal to CHUNK_MAX_TOKENS")
        if self.recall_default_top_k > self.recall_max_top_k:
            raise ValueError("RECALL_DEFAULT_TOP_K must be less than or equal to RECALL_MAX_TOP_K")
        if self.entity_merge_similarity_threshold < self.entity_dedup_similarity_threshold:
            raise ValueError(
                "ENTITY_MERGE_SIMILARITY_THRESHOLD must be greater than or equal to "
                "ENTITY_DEDUP_SIMILARITY_THRESHOLD"
            )
        if self.storage_backend == "s3":
            if not self.storage_s3_bucket or self.storage_s3_bucket.isspace():
                raise ValueError("STORAGE_S3_BUCKET is required when STORAGE_BACKEND=s3")
            if not self.storage_s3_region or self.storage_s3_region.isspace():
                raise ValueError("STORAGE_S3_REGION is required when STORAGE_BACKEND=s3")
        # Pair consistency: never mix an explicit key with a chain-resolved
        # secret. Credentials remain fully optional (ADR-0011 D16 -- the
        # standard provider chain is a supported, unconfigured default), but a
        # half-supplied explicit pair is a configuration mistake, not a valid
        # "use the chain" signal.
        access_key_present = not _raw_secret_is_absent(self.storage_s3_access_key_id)
        secret_key_present = not _raw_secret_is_absent(self.storage_s3_secret_access_key)
        if access_key_present != secret_key_present:
            raise ValueError(
                "STORAGE_S3_ACCESS_KEY_ID and STORAGE_S3_SECRET_ACCESS_KEY must be set together"
            )
        if not _raw_secret_is_absent(self.storage_s3_session_token) and not (
            access_key_present and secret_key_present
        ):
            raise ValueError(
                "STORAGE_S3_SESSION_TOKEN requires STORAGE_S3_ACCESS_KEY_ID and "
                "STORAGE_S3_SECRET_ACCESS_KEY to also be set"
            )

        return self


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    # pydantic-settings supports _env_file at runtime, but mypy does not expose
    # BaseSettings' dynamic constructor kwargs in the generated Settings signature.
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def sanitize_fingerprint_url(value: str) -> str:
    parsed = urlparse(value)
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse((parsed.scheme, netloc, parsed.path.rstrip("/"), "", "", ""))


def build_config_fingerprint_payload(
    settings: Settings,
    prompt_versions: Mapping[str, str] | None = None,
) -> ConfigFingerprintPayload:
    prompt_version_payload = dict(sorted((prompt_versions or DEFAULT_PROMPT_VERSIONS).items()))

    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "llm": {
            "base_url": sanitize_fingerprint_url(settings.llm_base_url),
            "model": settings.llm_model,
        },
        "embeddings": {
            "base_url": sanitize_fingerprint_url(settings.embedding_base_url),
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        "chunking": {
            "max_tokens": settings.chunk_max_tokens,
            "overlap_tokens": settings.chunk_overlap_tokens,
            "min_tokens": settings.chunk_min_tokens,
        },
        "retrieval": {
            "vector_top_k": settings.recall_vector_top_k,
            "lexical_top_k": settings.recall_lexical_top_k,
            "graph_seed_top_k": settings.recall_graph_seed_top_k,
            "graph_depth": settings.recall_graph_depth,
            "graph_max_nodes": settings.recall_graph_max_nodes,
            "default_top_k": settings.recall_default_top_k,
            "max_top_k": settings.recall_max_top_k,
            "rrf_k": settings.recall_rrf_k,
        },
        "improve": {
            "entity_dedup_similarity_threshold": settings.entity_dedup_similarity_threshold,
            "entity_merge_similarity_threshold": settings.entity_merge_similarity_threshold,
        },
        "prompt_versions": prompt_version_payload,
    }


def canonical_config_fingerprint_payload(payload: ConfigFingerprintPayload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_config_fingerprint(
    settings: Settings,
    prompt_versions: Mapping[str, str] | None = None,
) -> str:
    payload = build_config_fingerprint_payload(settings, prompt_versions=prompt_versions)
    canonical_payload = canonical_config_fingerprint_payload(payload)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
