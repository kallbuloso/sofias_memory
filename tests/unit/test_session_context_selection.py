from __future__ import annotations

from uuid import uuid4

from sofias_memory.domain import (
    SessionContextCandidate,
    render_session_context_block,
    render_session_context_entry,
    select_session_context,
)


def candidate(role: str, content: str) -> SessionContextCandidate:
    return SessionContextCandidate(entry_id=uuid4(), role=role, content=content)


def test_render_includes_only_role_and_content() -> None:
    entry = candidate("user", "hello")
    rendered = render_session_context_entry(entry)

    assert rendered == "role: user\ncontent: hello"
    assert str(entry.entry_id) not in rendered


def test_select_returns_empty_when_newest_alone_does_not_fit() -> None:
    newest = candidate("user", "x" * 100)
    older_smaller = candidate("user", "y")

    selected = select_session_context([newest, older_smaller], max_entries=10, max_chars=50)

    assert selected == []


def test_select_stops_on_first_non_fit_without_skipping_to_older_smaller() -> None:
    newest = candidate("user", "a")
    middle_too_big = candidate("user", "b" * 100)
    older_small = candidate("user", "c")

    block_len = len(render_session_context_entry(newest))
    selected = select_session_context(
        [newest, middle_too_big, older_small],
        max_entries=10,
        max_chars=block_len,
    )

    assert [c.entry_id for c in selected] == [newest.entry_id]


def test_select_respects_max_entries_and_returns_oldest_to_newest() -> None:
    entries_newest_first = [candidate("user", f"turn-{i}") for i in range(5)]

    selected = select_session_context(entries_newest_first, max_entries=2, max_chars=100_000)

    assert [c.entry_id for c in selected] == [
        entries_newest_first[1].entry_id,
        entries_newest_first[0].entry_id,
    ]


def test_select_exact_char_boundary_fits_one_char_over_does_not() -> None:
    entry = candidate("user", "hello")
    exact = len(render_session_context_entry(entry))

    fits = select_session_context([entry], max_entries=10, max_chars=exact)
    does_not_fit = select_session_context([entry], max_entries=10, max_chars=exact - 1)

    assert [c.entry_id for c in fits] == [entry.entry_id]
    assert does_not_fit == []


def test_select_accounts_for_separator_between_entries() -> None:
    first = candidate("user", "a")
    second = candidate("user", "b")
    first_block = render_session_context_entry(first)
    second_block = render_session_context_entry(second)
    combined_without_separator = len(first_block) + len(second_block)

    # Exactly enough for both blocks but no separator -> only the newest fits.
    only_newest = select_session_context(
        [second, first], max_entries=10, max_chars=combined_without_separator
    )
    assert [c.entry_id for c in only_newest] == [second.entry_id]

    combined_with_separator = combined_without_separator + len("\n\n")
    both = select_session_context(
        [second, first], max_entries=10, max_chars=combined_with_separator
    )
    assert [c.entry_id for c in both] == [first.entry_id, second.entry_id]


def test_select_never_truncates_content() -> None:
    long_content = "z" * 5000
    entry = candidate("user", long_content)

    selected = select_session_context([entry], max_entries=10, max_chars=10_000)

    assert selected[0].content == long_content
    assert len(selected[0].content) == 5000


def test_select_empty_candidates_returns_empty() -> None:
    assert select_session_context([], max_entries=10, max_chars=1000) == []


def test_render_block_joins_oldest_to_newest_with_separator() -> None:
    older = candidate("user", "first")
    newer = candidate("assistant", "second")

    block = render_session_context_block([older, newer])

    assert block == "role: user\ncontent: first\n\nrole: assistant\ncontent: second"
