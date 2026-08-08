from __future__ import annotations

import logging as stdlib_logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TextIO, cast
from urllib.parse import SplitResult, parse_qsl, urlsplit, urlunsplit

import structlog
from pydantic import SecretStr
from structlog.stdlib import BoundLogger, ProcessorFormatter
from structlog.typing import EventDict, WrappedLogger

REDACTED = "[REDACTED]"

LOG_CONTEXT_FIELDS = frozenset(
    {
        "request_id",
        "run_id",
        "dataset_id",
        "source_id",
        "document_id",
        "step",
    }
)

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "x_api_key",
        "authorization",
        "llm_api_key",
        "embedding_api_key",
        "database_url",
        "db_password",
        "neo4j_password",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)

SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "password",
    "passwd",
    "secret",
    "access_token",
    "refresh_token",
)

CONTENT_FIELD_NAMES = frozenset(
    {
        "body",
        "chunk",
        "chunks",
        "content",
        "document",
        "document_content",
        "embedding",
        "embeddings",
        "llm_payload",
        "llm_request_payload",
        "llm_response_payload",
        "messages",
        "prompt",
        "request_body",
        "response_body",
    }
)

_NORMALIZE_FIELD_PATTERN = re.compile(r"[^a-z0-9]+")
_SOFIAS_MEMORY_HANDLER = "_sofias_memory_structlog_handler"


def get_logger(name: str | None = None) -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger(name))


def configure_logging(log_level: str | int = "INFO", stream: TextIO | None = None) -> None:
    level = _resolve_log_level(log_level)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp")

    formatter = ProcessorFormatter(
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            timestamper,
        ],
        processors=[
            redact_log_event,
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = stdlib_logging.StreamHandler(stream or sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    setattr(handler, _SOFIAS_MEMORY_HANDLER, True)

    root_logger = stdlib_logging.getLogger()
    for existing_handler in list(root_logger.handlers):
        if getattr(existing_handler, _SOFIAS_MEMORY_HANDLER, False):
            root_logger.removeHandler(existing_handler)

    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            timestamper,
            redact_log_event,
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def bind_log_context(**context: object) -> None:
    structlog.contextvars.bind_contextvars(**_validated_log_context(context))


@contextmanager
def bound_log_context(**context: object) -> Iterator[None]:
    with structlog.contextvars.bound_contextvars(**_validated_log_context(context)):
        yield


def get_log_context() -> dict[str, object]:
    return dict(cast(Mapping[str, object], structlog.contextvars.get_contextvars()))


def unbind_log_context(*fields: str) -> None:
    unknown_fields = set(fields) - LOG_CONTEXT_FIELDS
    if unknown_fields:
        names = ", ".join(sorted(unknown_fields))
        raise ValueError(f"unsupported log context fields: {names}")

    structlog.contextvars.unbind_contextvars(*fields)


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()


def _validated_log_context(context: Mapping[str, object]) -> dict[str, object]:
    unknown_fields = set(context) - LOG_CONTEXT_FIELDS
    if unknown_fields:
        fields = ", ".join(sorted(unknown_fields))
        raise ValueError(f"unsupported log context fields: {fields}")

    return {field: value for field, value in context.items() if value is not None}


def redact_log_event(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    del logger, method_name
    return cast(EventDict, redact_sensitive_data(event_dict))


def redact_sensitive_data(value: object) -> object:
    return _redact_value(value, field_name=None)


def _redact_value(value: object, field_name: str | None) -> object:
    if field_name is not None and _is_redacted_field(field_name):
        return REDACTED

    if isinstance(value, SecretStr):
        return REDACTED

    if isinstance(value, Mapping):
        return {key: _redact_value(item, _field_name_from_key(key)) for key, item in value.items()}

    if isinstance(value, list):
        return [_redact_value(item, field_name=None) for item in value]

    if isinstance(value, tuple):
        return tuple(_redact_value(item, field_name=None) for item in value)

    if isinstance(value, str):
        return _redact_url_credentials(value)

    return value


def _field_name_from_key(key: object) -> str:
    return str(key)


def _is_redacted_field(field_name: str) -> bool:
    normalized = _normalize_field_name(field_name)
    return (
        normalized in SENSITIVE_FIELD_NAMES
        or normalized in CONTENT_FIELD_NAMES
        or any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS)
    )


def _normalize_field_name(field_name: str) -> str:
    return _NORMALIZE_FIELD_PATTERN.sub("_", field_name.lower()).strip("_")


def _redact_url_credentials(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value

    netloc = parsed.netloc
    if "@" in netloc:
        netloc = _redact_url_netloc(parsed)

    query = _redact_url_query(parsed.query)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_url_netloc(parsed_url: SplitResult) -> str:
    host = parsed_url.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed_url.port is not None:
        host = f"{host}:{parsed_url.port}"

    if parsed_url.username is None:
        return host

    if parsed_url.password is None:
        return f"{parsed_url.username}@{host}"

    return f"{parsed_url.username}:***@{host}"


def _redact_url_query(query: str) -> str:
    if not query:
        return query

    parameters = parse_qsl(query, keep_blank_values=True)
    return "&".join(
        f"{key}={REDACTED if _is_redacted_field(key) else value}" for key, value in parameters
    )


def _resolve_log_level(log_level: str | int) -> int:
    if isinstance(log_level, int):
        return log_level

    level = stdlib_logging.getLevelName(log_level.upper())
    if not isinstance(level, int):
        raise ValueError(f"unsupported log level: {log_level}")

    return level
