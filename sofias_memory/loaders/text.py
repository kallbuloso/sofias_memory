"""Deterministic text preparation for direct text and textual file ingest."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import StringIO
from pathlib import PurePosixPath

CSV_FILE_MIME_TYPE = "text/csv"
HTML_FILE_MIME_TYPE = "text/html"
JSON_FILE_MIME_TYPE = "application/json"
TEXT_FILE_MIME_TYPE = "text/plain"
MARKDOWN_FILE_MIME_TYPE = "text/markdown"
SUPPORTED_TEXT_FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".json", ".csv", ".html", ".htm"}
)
STORAGE_EXTENSION_BY_SOURCE_EXTENSION = {
    ".txt": ".txt",
    ".md": ".md",
    ".markdown": ".md",
    ".json": ".json",
    ".csv": ".csv",
    ".html": ".html",
    ".htm": ".html",
}
MIME_TYPE_BY_SOURCE_EXTENSION = {
    ".txt": TEXT_FILE_MIME_TYPE,
    ".md": MARKDOWN_FILE_MIME_TYPE,
    ".markdown": MARKDOWN_FILE_MIME_TYPE,
    ".json": JSON_FILE_MIME_TYPE,
    ".csv": CSV_FILE_MIME_TYPE,
    ".html": HTML_FILE_MIME_TYPE,
    ".htm": HTML_FILE_MIME_TYPE,
}
ALLOWED_CONTROL_CHARACTERS = frozenset({"\t", "\n", "\r", "\f"})
HTML_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
HTML_IGNORED_TAGS = frozenset({"script", "style", "noscript"})


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
    """Validate a textual upload and return decoded normalized text data."""

    original_filename = sanitize_upload_filename(filename)
    source_extension = PurePosixPath(original_filename).suffix.lower()
    if source_extension not in SUPPORTED_TEXT_FILE_EXTENSIONS:
        raise TextFileLoadError("Unsupported file extension.")

    decoded_text = decode_utf8_text(original_bytes)
    normalized_text = normalized_text_for_extension(source_extension, decoded_text)

    if not normalized_text:
        raise TextFileLoadError("File content must not be empty.")
    if contains_disallowed_control_characters(normalized_text):
        raise TextFileLoadError("File content is not supported textual data.")

    return PreparedTextFile(
        original_filename=original_filename,
        storage_extension=STORAGE_EXTENSION_BY_SOURCE_EXTENSION[source_extension],
        mime_type=MIME_TYPE_BY_SOURCE_EXTENSION[source_extension],
        text=prepare_text_bytes(original_bytes, decoded_text=normalized_text),
    )


def decode_utf8_text(original_bytes: bytes) -> str:
    try:
        return original_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TextFileLoadError("File content must be valid UTF-8 text.") from exc


def normalized_text_for_extension(source_extension: str, decoded_text: str) -> str:
    if source_extension == ".json":
        return normalize_json_text(decoded_text)
    if source_extension == ".csv":
        return normalize_csv_text(decoded_text)
    if source_extension in {".html", ".htm"}:
        return normalize_html_text(decoded_text)
    decoded_text = decoded_text.replace("\x00", "")
    if contains_disallowed_control_characters(decoded_text):
        raise TextFileLoadError("File content is not supported textual data.")
    return normalize_line_endings(decoded_text)


def normalize_json_text(decoded_text: str) -> str:
    try:
        parsed = json.loads(decoded_text)
    except json.JSONDecodeError as exc:
        raise TextFileLoadError("File content must be valid JSON.") from exc
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def normalize_csv_text(decoded_text: str) -> str:
    normalized_text = normalize_line_endings(decoded_text.replace("\x00", ""))
    if not normalized_text.strip():
        raise TextFileLoadError("CSV file content must not be empty.")
    try:
        rows = list(csv.reader(StringIO(normalized_text), strict=True))
    except csv.Error as exc:
        raise TextFileLoadError("File content must be valid CSV.") from exc
    if not rows:
        raise TextFileLoadError("CSV file content must not be empty.")
    if contains_disallowed_control_characters(normalized_text):
        raise TextFileLoadError("File content is not supported textual data.")
    return normalized_text


def normalize_html_text(decoded_text: str) -> str:
    decoded_text = decoded_text.replace("\x00", "")
    if contains_disallowed_control_characters(decoded_text):
        raise TextFileLoadError("File content is not supported textual data.")
    parser = VisibleHTMLTextParser()
    try:
        parser.feed(decoded_text)
        parser.close()
    except Exception as exc:
        raise TextFileLoadError("File content must be valid HTML text.") from exc
    text = parser.visible_text()
    if not text:
        raise TextFileLoadError("HTML file content must contain visible text.")
    return text


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


class VisibleHTMLTextParser(HTMLParser):
    """Extract deterministic visible text from simple HTML without external dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.lower()
        if normalized_tag in HTML_IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if normalized_tag in HTML_BLOCK_TAGS:
            self._separator()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in HTML_IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if normalized_tag in HTML_BLOCK_TAGS:
            self._separator()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        collapsed = " ".join(data.split())
        if not collapsed:
            return
        self._parts.append(collapsed)

    def visible_text(self) -> str:
        lines: list[str] = []
        current: list[str] = []
        for part in self._parts:
            if part == "\n":
                if current:
                    lines.append(join_inline_text(current))
                    current = []
                continue
            current.append(part)
        if current:
            lines.append(join_inline_text(current))
        return "\n".join(line for line in lines if line).strip()

    def _separator(self) -> None:
        if self._parts and self._parts[-1] != "\n":
            self._parts.append("\n")


def join_inline_text(parts: list[str]) -> str:
    text = ""
    for part in parts:
        if not text:
            text = part
        elif part[:1] in {".", ",", ";", ":", "!", "?", ")", "]", "}"}:
            text = f"{text}{part}"
        else:
            text = f"{text} {part}"
    return text
