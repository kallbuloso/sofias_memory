"""Stable domain enums shared by persistence and application code."""

from __future__ import annotations

from enum import StrEnum


class DatasetStatus(StrEnum):
    """Lifecycle status for datasets."""

    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
