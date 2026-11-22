"""Regression tests for PDF text extraction (SEC-01).

SEC-01 upgrades ``pypdf`` to clear runtime security advisories. These tests pin
the loader behavior the upgrade must preserve -- ``PdfReader(strict=False)``,
encrypted-PDF handling, single/multi-page text extraction and the error
contract for empty, invalid and truncated input -- using only small synthetic
PDFs built in memory (no external or untrusted samples).
"""

from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from sofias_memory.loaders.text import (
    PDF_FILE_MIME_TYPE,
    TextFileLoadError,
    extract_pdf_text,
    prepare_text_file_content,
)

READABLE_PDF_ERROR = "File content must be a readable textual PDF."
ENCRYPTED_PDF_ERROR = "PDF file is encrypted and cannot be read without a password."
NO_TEXT_PDF_ERROR = "PDF file must contain extractable text."


def build_pdf(page_texts: list[str | None]) -> bytes:
    """Build a minimal valid PDF with one Helvetica text line per page.

    A ``None`` entry produces a page without any content stream (no text).
    """

    page_count = len(page_texts)
    font_number = 3
    first_page_number = 4
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        font_number: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[str] = []
    next_number = first_page_number + page_count
    for index, text in enumerate(page_texts):
        page_number = first_page_number + index
        kids.append(f"{page_number} 0 R")
        page = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_number} 0 R >> >>"
        )
        if text is not None:
            stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
            objects[next_number] = (
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            page += f" /Contents {next_number} 0 R"
            next_number += 1
        objects[page_number] = (page + " >>").encode("latin-1")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {page_count} >>".encode(
        "latin-1"
    )

    output = BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = output.tell()
        output.write(f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n")
    xref_offset = output.tell()
    size = max(objects) + 1
    output.write(f"xref\n0 {size}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for number in range(1, size):
        output.write(f"{offsets[number]:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return output.getvalue()


def encrypt_pdf(pdf_bytes: bytes, *, user_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes)))
    writer.encrypt(
        user_password=user_password,
        owner_password="synthetic-owner-password",
        algorithm="RC4-128",
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_single_page_text_is_extracted() -> None:
    assert extract_pdf_text(build_pdf(["Synthetic SEC-01 page"])) == "Synthetic SEC-01 page"


def test_multi_page_text_is_joined_in_page_order() -> None:
    pdf_bytes = build_pdf(["First synthetic page", "Second synthetic page", "Third page"])

    assert extract_pdf_text(pdf_bytes) == (
        "First synthetic page\n\nSecond synthetic page\n\nThird page"
    )


def test_pages_without_text_are_skipped() -> None:
    assert extract_pdf_text(build_pdf([None, "Only text page", None])) == "Only text page"


def test_pdf_without_any_text_is_rejected() -> None:
    with pytest.raises(TextFileLoadError, match=NO_TEXT_PDF_ERROR):
        extract_pdf_text(build_pdf([None]))


def test_encrypted_pdf_with_empty_user_password_is_read() -> None:
    pdf_bytes = encrypt_pdf(build_pdf(["Encrypted synthetic text"]), user_password="")

    assert PdfReader(BytesIO(pdf_bytes)).is_encrypted
    assert extract_pdf_text(pdf_bytes) == "Encrypted synthetic text"


def test_encrypted_pdf_requiring_a_password_is_rejected() -> None:
    pdf_bytes = encrypt_pdf(build_pdf(["Locked synthetic text"]), user_password="user-secret")

    with pytest.raises(TextFileLoadError, match=ENCRYPTED_PDF_ERROR):
        extract_pdf_text(pdf_bytes)


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.4\n",
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog",
    ],
    ids=["empty", "not-pdf", "header-only", "unterminated-object"],
)
def test_invalid_pdf_is_rejected_with_the_readable_pdf_error(invalid_bytes: bytes) -> None:
    with pytest.raises(TextFileLoadError, match=READABLE_PDF_ERROR):
        extract_pdf_text(invalid_bytes)


@pytest.mark.parametrize("keep_fraction", [0.25, 0.5, 0.75, 0.95])
def test_truncated_pdf_never_escapes_the_loader_error_contract(keep_fraction: float) -> None:
    pdf_bytes = build_pdf(["Truncated synthetic page one", "Truncated synthetic page two"])
    truncated = pdf_bytes[: int(len(pdf_bytes) * keep_fraction)]

    try:
        text = extract_pdf_text(truncated)
    except TextFileLoadError as exc:
        assert str(exc) in {READABLE_PDF_ERROR, NO_TEXT_PDF_ERROR}
    else:
        assert text.strip()


def test_prepare_text_file_content_routes_pdf_through_the_pdf_loader() -> None:
    prepared = prepare_text_file_content("synthetic.pdf", build_pdf(["Prepared PDF text"]))

    assert prepared.mime_type == PDF_FILE_MIME_TYPE
    assert prepared.storage_extension == ".pdf"
    assert prepared.original_filename == "synthetic.pdf"


def test_prepare_text_file_content_rejects_an_unreadable_pdf() -> None:
    with pytest.raises(TextFileLoadError, match=READABLE_PDF_ERROR):
        prepare_text_file_content("synthetic.pdf", b"not a pdf at all")
