"""``SessionEntry.external_id`` normalization primitive.

Deliberately separate from :mod:`sofias_memory.domain.session_id`: a
Session's external ``session_id`` and a SessionEntry's ``external_id`` are
different concepts (instance-wide external key vs. per-Session correlation/
idempotency identity) that only coincidentally share the same 255-character
bound today. Coupling them to the same constant would make a future,
independent change to either bound silently affect the other. This module
has no dependency on FastAPI, SQLAlchemy, or Neo4j, and no reference to any
specific caller (e.g. Sofia's Assistant) or concept (e.g. "Turn").
"""

from __future__ import annotations

SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH = 255
"""Maximum SessionEntry ``external_id`` length, applied after trimming."""


class InvalidSessionEntryExternalIdError(ValueError):
    """A caller-supplied SessionEntry ``external_id`` fails the shared contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid external_id: {reason}")


def normalize_session_entry_external_id(value: str | None) -> str | None:
    """Normalize a caller-supplied SessionEntry ``external_id``.

    ``None`` -> ``None``. Otherwise the value is trimmed, case is preserved
    (never slugified, never lowercased), and the trimmed value must be
    non-empty and at most :data:`SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH`
    characters -- both violations raise
    :class:`InvalidSessionEntryExternalIdError`.
    """

    if value is None:
        return None

    stripped = value.strip()
    if not stripped:
        raise InvalidSessionEntryExternalIdError("external_id must not be empty or whitespace-only")
    if len(stripped) > SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH:
        raise InvalidSessionEntryExternalIdError(
            f"external_id must be at most {SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH} "
            "characters after trim"
        )
    return stripped
