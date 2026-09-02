"""Unit tests for the centralized MIME -> canonical storage extension
mapping ADR-0011 D6/D35 requires (STORAGE-001).

``STORAGE_EXTENSION_BY_MIME_TYPE`` is not new behavior -- it is derived from
the two extension-keyed tables that already govern current file/URL Remember
behavior. These tests prove the mapping is a well-defined function (every
mime type resolves to exactly one storage extension) and that it agrees with
every currently-supported source extension, including the alias pairs
(``.markdown``/``.md``, ``.htm``/``.html``).
"""

from __future__ import annotations

import pytest

from sofias_memory.loaders.text import (
    CSV_FILE_MIME_TYPE,
    DOCX_FILE_MIME_TYPE,
    HTML_FILE_MIME_TYPE,
    JSON_FILE_MIME_TYPE,
    MARKDOWN_FILE_MIME_TYPE,
    MIME_TYPE_BY_SOURCE_EXTENSION,
    PDF_FILE_MIME_TYPE,
    STORAGE_EXTENSION_BY_MIME_TYPE,
    STORAGE_EXTENSION_BY_SOURCE_EXTENSION,
    TEXT_FILE_MIME_TYPE,
    canonical_storage_extension_for_mime_type,
)
from sofias_memory.services.remember import TEXT_MIME_TYPE, TEXT_STORAGE_EXTENSION


@pytest.mark.parametrize(
    ("source_extension", "expected_storage_extension"),
    sorted(STORAGE_EXTENSION_BY_SOURCE_EXTENSION.items()),
)
def test_every_supported_source_extension_resolves_via_its_mime_type(
    source_extension: str, expected_storage_extension: str
) -> None:
    mime_type = MIME_TYPE_BY_SOURCE_EXTENSION[source_extension]

    assert canonical_storage_extension_for_mime_type(mime_type) == expected_storage_extension


def test_alias_extensions_share_one_canonical_storage_extension() -> None:
    # .markdown and .md both mean text/markdown -> canonical .md; .htm and
    # .html both mean text/html -> canonical .html. Proves the mapping is a
    # well-defined function, not merely non-crashing.
    assert canonical_storage_extension_for_mime_type(MARKDOWN_FILE_MIME_TYPE) == ".md"
    assert canonical_storage_extension_for_mime_type(HTML_FILE_MIME_TYPE) == ".html"


@pytest.mark.parametrize(
    ("mime_type", "expected_storage_extension"),
    [
        (TEXT_FILE_MIME_TYPE, ".txt"),
        (JSON_FILE_MIME_TYPE, ".json"),
        (CSV_FILE_MIME_TYPE, ".csv"),
        (PDF_FILE_MIME_TYPE, ".pdf"),
        (DOCX_FILE_MIME_TYPE, ".docx"),
    ],
)
def test_non_alias_mime_types_resolve_to_their_single_extension(
    mime_type: str, expected_storage_extension: str
) -> None:
    assert canonical_storage_extension_for_mime_type(mime_type) == expected_storage_extension


def test_unmapped_mime_type_fails_closed_never_guesses() -> None:
    with pytest.raises(ValueError, match="No canonical storage extension"):
        canonical_storage_extension_for_mime_type("application/x-unknown")


def test_mapping_is_a_well_defined_function_no_conflicting_extension() -> None:
    # Re-derive independently of the module's own (already-verified-at-import)
    # construction, so a future edit to either source table that breaks
    # bijectivity is caught here too, not only by the module import itself.
    seen: dict[str, str] = {}
    for source_extension, mime_type in MIME_TYPE_BY_SOURCE_EXTENSION.items():
        storage_extension = STORAGE_EXTENSION_BY_SOURCE_EXTENSION[source_extension]
        if mime_type in seen:
            assert seen[mime_type] == storage_extension, (
                f"mime type {mime_type!r} maps to inconsistent storage extensions"
            )
        seen[mime_type] = storage_extension
    assert seen == STORAGE_EXTENSION_BY_MIME_TYPE


def test_direct_text_remember_constants_agree_with_the_centralized_mapping() -> None:
    # services.remember's TEXT_MIME_TYPE/TEXT_STORAGE_EXTENSION constants
    # (used for source_kind="text" Remember, which never goes through
    # loaders.text's file-extension dispatch at all) must still resolve
    # through the same centralized mapping D35 will rely on later.
    assert canonical_storage_extension_for_mime_type(TEXT_MIME_TYPE) == TEXT_STORAGE_EXTENSION
