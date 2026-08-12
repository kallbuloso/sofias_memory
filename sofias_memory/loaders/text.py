"""Deterministic text preparation for direct text and text-file ingest."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

TEXT_FILE_MIME_TYPE = "text/plain"
MARKDOWN_FILE_MIME_TYPE = "text/markdown"
SUPPORTED_TEXT_FILE_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})
STORAGE_EXTENSION_BY_SOURCE_EXTENSION = {
    ".txt": ".txt",
    ".md": ".md",
    ".markdown": ".md",
}
MIME_TYPE_BY_SOURCE_EXTENSION = {
    ".txt": TEXT_FILE_MIME_TYPE,
    ".md": MARKDOWN_FILE_MIME_TYPE,
    ".markdown": MARKDOWN_FILE_MIME_TYPE,
}
ALLOWED_CONTROL_CHARACTERS = frozenset({"\t", "\n", "\r", "\f"})


@dataclass(frozen=True)
class PreparedText:
    """Original and normalized forms of a direct text input."""

    original_bytes: bytes
    normalized_text: str
    content_sha256: str
    normalized_sha256: str
    byte_size: int


@dataclass(frozen=True)
class PreparedTextFile:
    """A validated plain-text upload ready for remember ingest."""

    original_filename: str
    storage_extension: str
    mime_type: str
    text: PreparedText


class TextFileLoadError(ValueError):
    """Raised when an uploaded text file cannot be safely loaded."""


def prepare_text_content(content: str) -> PreparedText:
    """Encode original text and normalize line endings for persistence."""

    original_bytes = content.encode("utf-8")
    return prepare_text_bytes(original_bytes, decoded_text=content)


def prepare_text_file_content(filename: str | None, original_bytes: bytes) -> PreparedTextFile:
    """Validate a TXT/Markdown upload and return decoded normalized text data."""

    original_filename = sanitize_upload_filename(filename)
    source_extension = PurePosixPath(original_filename).suffix.lower()
    if source_extension not in SUPPORTED_TEXT_FILE_EXTENSIONS:
        raise TextFileLoadError("Unsupported file extension.")

    try:
        decoded_text = original_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TextFileLoadError("File content must be valid UTF-8 text.") from exc

    decoded_text = decoded_text.replace("\x00", "")
    if not decoded_text:
        raise TextFileLoadError("File content must not be empty.")
    if contains_disallowed_control_characters(decoded_text):
        raise TextFileLoadError("File content is not supported textual data.")

    return PreparedTextFile(
        original_filename=original_filename,
        storage_extension=STORAGE_EXTENSION_BY_SOURCE_EXTENSION[source_extension],
        mime_type=MIME_TYPE_BY_SOURCE_EXTENSION[source_extension],
        text=prepare_text_bytes(original_bytes, decoded_text=decoded_text),
    )


def prepare_text_bytes(original_bytes: bytes, *, decoded_text: str) -> PreparedText:
    """Hash original bytes and normalize a decoded textual representation."""

    normalized_text = normalize_line_endings(decoded_text)
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


def sanitize_upload_filename(filename: str | None) -> str:
    """Return a safe logical filename without trusting client path components."""

    candidate = (filename or "").replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
    if not candidate or candidate in {".", ".."}:
        raise TextFileLoadError("Uploaded file must have a filename.")
    return candidate


def contains_disallowed_control_characters(content: str) -> bool:
    return any(
        character < " " and character not in ALLOWED_CONTROL_CHARACTERS for character in content
    )
