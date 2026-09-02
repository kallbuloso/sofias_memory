from __future__ import annotations

import pytest


_STORAGE_ENV_NAMES = (
    "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET",
    "STORAGE_S3_PREFIX",
    "STORAGE_S3_REGION",
    "STORAGE_S3_ENDPOINT_URL",
    "STORAGE_S3_ACCESS_KEY_ID",
    "STORAGE_S3_SECRET_ACCESS_KEY",
    "STORAGE_S3_SESSION_TOKEN",
    "STORAGE_S3_MAX_CONCURRENCY",
)


@pytest.fixture(autouse=True)
def isolate_unit_tests_from_storage_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit tests must never inherit live S3 configuration from the host."""

    for name in _STORAGE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)