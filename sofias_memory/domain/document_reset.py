"""Shared contract for the content-free document placeholder used by forget/cognify."""

from __future__ import annotations

from hashlib import sha256

RESET_DOCUMENT_METADATA_KEY = "forget_memory_reset"
RESET_DOCUMENT_METADATA_VERSION = "v1"
RESET_DOCUMENT_TEXT_SHA256 = sha256(b"").hexdigest()
RESET_DOCUMENT_TOKEN_COUNT = -1
RESET_DOCUMENT_TITLE = ""
RESET_DOCUMENT_LANGUAGE = "und"
