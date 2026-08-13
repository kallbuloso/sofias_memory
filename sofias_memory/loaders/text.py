"""Deterministic text preparation for direct text and textual file ingest."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import PurePosixPath

from docx import Document as DocxDocument
from pypdf import PdfReader

CSV_FILE_MIME_TYPE = "text/csv"
DOCX_FILE_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML_FILE_MIME_TYPE = "text/html"
JSON_FILE_MIME_TYPE = "application/json"
PDF_FILE_MIME_TYPE = "application/pdf"
TEXT_FILE_MIME_TYPE = "text/plain"
MARKDOWN_FILE_MIME_TYPE = "text/markdown"
SUPPORTED_TEXT_FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".json", ".csv", ".html", ".htm", ".pdf", ".docx"}
)
STORAGE_EXTENSION_BY_SOURCE_EXTENSION = {
    ".txt": ".txt",
    ".md": ".md",
    ".markdown": ".md",
    ".json": ".json",
    ".csv": ".csv",
    ".html": ".html",
    ".htm": ".html",
    ".pdf": ".pdf",
    ".docx": ".docx",
}
MIME_TYPE_BY_SOURCE_EXTENSION = {
    ".txt": TEXT_FILE_MIME_TYPE,
    ".md": MARKDOWN_FILE_MIME_TYPE,
    ".markdown": MARKDOWN_FILE_MIME_TYPE,
    ".json": JSON_FILE_MIME_TYPE,
    ".csv": CSV_FILE_MIME_TYPE,
    ".html": HTML_FILE_MIME_TYPE,
    ".htm": HTML_FILE_MIME_TYPE,
    ".pdf": PDF_FILE_MIME_TYPE,
    ".docx": DOCX_FILE_MIME_TYPE,
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

    if source_extension == ".pdf":
        normalized_text = extract_pdf_text(original_bytes)
    elif source_extension == ".docx":
        normalized_text = extract_docx_text(original_bytes)
    else:
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


def extract_pdf_text(original_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(original_bytes), strict=False)
        if reader.is_encrypted:
            decrypt_result = reader.decrypt("")
            if decrypt_result == 0:
                raise TextFileLoadError(
                    "PDF file is encrypted and cannot be read without a password."
                )

        page_texts = [
            normalize_line_endings(page.extract_text() or "").strip() for page in reader.pages
        ]
    except TextFileLoadError:
        raise
    except Exception as exc:
        raise TextFileLoadError("File content must be a readable textual PDF.") from exc

    extracted_text = "\n\n".join(page_text for page_text in page_texts if page_text)
    if not extracted_text:
        raise TextFileLoadError("PDF file must contain extractable text.")
    return extracted_text


def extract_docx_text(original_bytes: bytes) -> str:
    try:
        document = DocxDocument(BytesIO(original_bytes))
        blocks = list(iter_docx_text_blocks(document))
    except Exception as exc:
        raise TextFileLoadError("File content must be a readable DOCX document.") from exc

    extracted_text = "\n".join(block for block in blocks if block.strip()).strip()
    if not extracted_text:
        raise TextFileLoadError("DOCX file must contain text.")
    return normalize_line_endings(extracted_text)


def iter_docx_text_blocks(document: object) -> list[str]:
    if hasattr(document, "iter_inner_content"):
        return [
            text for element in document.iter_inner_content() for text in docx_element_text(element)
        ]

    paragraphs = getattr(document, "paragraphs", ())
    tables = getattr(document, "tables", ())
    return [
        *(paragraph.text for paragraph in paragraphs if paragraph.text.strip()),
        *(docx_table_text(table) for table in tables if docx_table_text(table).strip()),
    ]


def docx_element_text(element: object) -> list[str]:
    rows = getattr(element, "rows", None)
    if rows is not None:
        table_text = docx_table_text(element)
        return [table_text] if table_text.strip() else []

    text = getattr(element, "text", "")
    if isinstance(text, str) and text.strip():
        return [text.strip()]
    return []


def docx_table_text(table: object) -> str:
    rows = getattr(table, "rows", ())
    row_texts: list[str] = []
    for row in rows:
        cells = getattr(row, "cells", ())
        cell_texts = [normalize_docx_cell_text(cell.text) for cell in cells if cell.text.strip()]
        if cell_texts:
            row_texts.append("\t".join(cell_texts))
    return "\n".join(row_texts)


def normalize_docx_cell_text(text: str) -> str:
    return " ".join(normalize_line_endings(text).split())


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
