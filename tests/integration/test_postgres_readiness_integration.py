from __future__ import annotations

import os

import pytest

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker

POSTGRES_READINESS_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_READINESS_TESTS"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_readiness_checker_against_real_database() -> None:
    if os.environ.get(POSTGRES_READINESS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_READINESS_ENV}=1 to run PostgreSQL readiness tests")

    checker = PostgresReadinessChecker(load_settings())
    try:
        result = await checker.check()
    finally:
        await checker.dispose()

    assert result.ready is True
