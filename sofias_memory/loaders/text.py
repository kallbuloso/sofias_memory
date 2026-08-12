"""Deterministic text preparation for direct remember ingest."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class PreparedText:
    """Original and normalized forms of a direct text input."""

    original_bytes: bytes
    normalized_text: str
    content_sha256: str
    normalized_sha256: str
    byte_size: int


def prepare_text_content(content: str) -> PreparedText:
    """Encode original text and normalize line endings for persistence."""

    original_bytes = content.encode("utf-8")
    normalized_text = normalize_line_endings(content)
    normalized_bytes = normalized_text.encode("utf-8")
    return PreparedText(
        original_bytes=original_bytes,
        normalized_text=normalized_text,
        content_sha256=sha256(original_bytes).hexdigest(),
        normalized_sha256=sha256(normalized_bytes).hexdigest(),
        byte_size=len(original_bytes),
    )


def normalize_line_endings(content: str) -> str:
    """Normalize CRLF and CR line endings to LF without other text changes."""

    return content.replace("\r\n", "\n").replace("\r", "\n")
