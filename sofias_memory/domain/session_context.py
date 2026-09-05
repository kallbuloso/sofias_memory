"""Session Context selection/rendering for Recall RAG generation.

Pure functions only -- no FastAPI, SQLAlchemy, or Neo4j dependency. Operates
on a minimal candidate shape (entry_id/role/content), deliberately excluding
metadata, external_id, and created_at: the renderer must never leak anything
beyond what the Feature Contract allows into the RAG prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

SESSION_CONTEXT_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class SessionContextCandidate:
    """One SessionEntry candidate for Session Context selection."""

    entry_id: UUID
    role: str
    content: str


def render_session_context_entry(candidate: SessionContextCandidate) -> str:
    """Deterministic rendering of exactly ``role`` + ``content``.

    This never maps ``role`` onto a provider-privileged role -- the result
    is plain text data, embedded inside the caller's untrusted-context
    block like any other retrieved text, never a separate chat message.
    """

    return f"role: {candidate.role}\ncontent: {candidate.content}"


def select_session_context(
    candidates_newest_first: Sequence[SessionContextCandidate],
    *,
    max_entries: int,
    max_chars: int,
) -> list[SessionContextCandidate]:
    """Frozen selection algorithm (Feature Contract SS 18).

    Walk candidates newest -> oldest. For each, render its full block and
    check whether adding it (plus a separator, if not the first selected)
    would exceed ``max_chars``, or whether ``max_entries`` is already
    reached. The first candidate that does not fit **stops** the scan
    immediately -- it is never skipped in favor of an older, smaller one.
    No candidate is ever truncated; only whole blocks are considered.

    Returns the selected entries oldest -> newest (RAG generation order),
    or ``[]`` if even the single newest candidate does not fit alone.
    """

    selected_newest_first: list[SessionContextCandidate] = []
    total_chars = 0
    for candidate in candidates_newest_first:
        if len(selected_newest_first) >= max_entries:
            break
        block = render_session_context_entry(candidate)
        additional = (
            len(block)
            if not selected_newest_first
            else len(block) + len(SESSION_CONTEXT_BLOCK_SEPARATOR)
        )
        if total_chars + additional > max_chars:
            break
        selected_newest_first.append(candidate)
        total_chars += additional
    return list(reversed(selected_newest_first))


def render_session_context_block(selected_oldest_first: Sequence[SessionContextCandidate]) -> str:
    """Join already-selected (oldest -> newest) candidates into one block.

    This is exactly the budgeted text (per-entry blocks plus separators);
    any static outer wrapper (e.g. a "SESSION CONTEXT" heading) is added by
    the caller composing the full RAG prompt and is deliberately outside
    this function's budget accounting.
    """

    return SESSION_CONTEXT_BLOCK_SEPARATOR.join(
        render_session_context_entry(candidate) for candidate in selected_oldest_first
    )
