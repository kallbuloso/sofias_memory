"""Shared Native Cognitive Memory ``scope`` validation primitive (ADR-0016).

ADR-0016 and the v0.7.0 Feature Contract SS 5 freeze exactly two canonical
scope shapes -- ``global`` and ``project:<key>`` -- with exact-match
semantics and no wildcard, hierarchy, or tenant concept. Every Cognitive
Memory entry point must validate scope with exactly this one rule, so no
service/route may implement its own variant. This module has no dependency
on FastAPI, SQLAlchemy, or Neo4j.

Validation only, never silent normalization of the ``project:<key>``
suffix: the input is trimmed at the edges only, then matched as-is against
the frozen grammar -- an uppercase or otherwise malformed scope is rejected
outright, never lowercased or rewritten into a valid one (Feature Contract
SS 5.1).
"""

from __future__ import annotations

import re

GLOBAL_SCOPE = "global"
PROJECT_SCOPE_PREFIX = "project:"

_PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
"""1..128 chars: lowercase a-z, 0-9, '.', '_', '-'; first char a lowercase
letter or digit (Feature Contract SS 5.1)."""


class InvalidCognitiveMemoryScopeError(ValueError):
    """A caller-supplied Cognitive Memory ``scope`` fails the frozen v0.7 grammar."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory scope: {reason}")


def normalize_cognitive_memory_scope(value: str) -> str:
    """Trim edges only, then validate against ``global | project:<key>``.

    Returns the canonical scope unchanged when valid. Never lowercases or
    otherwise rewrites an invalid value into a valid one.
    """

    trimmed = value.strip()
    if not trimmed:
        raise InvalidCognitiveMemoryScopeError("scope must not be empty")
    if trimmed == GLOBAL_SCOPE:
        return trimmed
    if trimmed.startswith(PROJECT_SCOPE_PREFIX):
        key = trimmed[len(PROJECT_SCOPE_PREFIX) :]
        if not _PROJECT_KEY_PATTERN.fullmatch(key):
            raise InvalidCognitiveMemoryScopeError(
                "project key must be 1..128 characters of lowercase a-z, 0-9, "
                "'.', '_', '-', starting with a lowercase letter or digit"
            )
        return trimmed
    raise InvalidCognitiveMemoryScopeError("scope must be exactly 'global' or 'project:<key>'")
