"""Deterministic Skill resolution-text builder (ADR-0013, Feature Contract SS 7).

The single shared primitive for the text a Skill's ``resolution_embedding``
is computed from -- reused identically by SM-702 (structured create/create
revision), a future SM-703 (SKILL.md import), and consumed the same way by
a future SM-704 (resolve query embedding uses the same representation
family). No endpoint may concatenate this ad hoc.

Deliberately built from ``name`` + ``description`` + ``tags`` only --
``procedure``, ``license``, ``compatibility``, ``metadata``, and
``declared_tools`` are never included, preserving the Feature Contract's
metadata/procedure split: discovery embeddings represent *when to use* a
Skill, never its execution content.
"""

from __future__ import annotations

from collections.abc import Sequence


def build_skill_resolution_text(*, name: str, description: str, tags: Sequence[str]) -> str:
    """``name``, then ``description``, then (if non-empty) a comma-joined
    ``tags`` line -- one field per line, in this fixed order. ``tags`` is
    expected to already be canonicalized (deduplicated, sorted) by the
    caller, so the same tag set always produces the same text regardless of
    the order it was originally supplied in."""

    lines = [name, description]
    if tags:
        lines.append(", ".join(tags))
    return "\n".join(lines)
