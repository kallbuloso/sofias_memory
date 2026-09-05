"""Shared external ``session_id`` normalization primitive.

ADR-0012 SS Identity and the v0.3.0 Sessions Feature Contract SS 3/21 require
every Session-aware entry point (``POST /sessions``, Remember text/file/URL,
Recall) to normalize the caller-supplied external ``session_id`` with exactly
this one rule, so no endpoint may implement its own variant. This module has
no dependency on FastAPI, SQLAlchemy, or Neo4j.
"""

from __future__ import annotations

SESSION_ID_MAX_LENGTH = 255
"""Maximum external ``session_id`` length, applied after trimming."""


class InvalidSessionIdError(ValueError):
    """A caller-supplied external ``session_id`` fails the shared contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid session_id: {reason}")


def normalize_session_id(value: str | None) -> str | None:
    """Normalize a caller-supplied external ``session_id``.

    ``None`` -> ``None``. Otherwise the value is trimmed, case is preserved
    (never slugified, never lowercased), and the trimmed value must be
    non-empty and at most :data:`SESSION_ID_MAX_LENGTH` characters -- both
    violations raise :class:`InvalidSessionIdError`.
    """

    if value is None:
        return None

    stripped = value.strip()
    if not stripped:
        raise InvalidSessionIdError("session_id must not be empty or whitespace-only")
    if len(stripped) > SESSION_ID_MAX_LENGTH:
        raise InvalidSessionIdError(
            f"session_id must be at most {SESSION_ID_MAX_LENGTH} characters after trim"
        )
    return stripped
