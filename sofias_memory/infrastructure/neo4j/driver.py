"""Async Neo4j driver lifecycle primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, cast

from neo4j import AsyncDriver, AsyncGraphDatabase

from sofias_memory.config import Settings

Neo4jAuth = tuple[str, str]


class AsyncNeo4jDriver(Protocol):
    """Small subset of the official async driver used by this layer."""

    async def verify_connectivity(self, **config: object) -> None:
        """Verify network connectivity when explicitly requested."""

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> object:
        """Execute an explicit Neo4j query against the configured database."""

    async def close(self) -> None:
        """Close driver resources."""


Neo4jDriverFactory = Callable[[str, Neo4jAuth], AsyncNeo4jDriver]


class Neo4jResource:
    """Application-owned Neo4j driver resource.

    Construction does not open a network connection. Connectivity is attempted
    only when ``verify_connectivity`` is called explicitly.
    """

    def __init__(self, driver: AsyncNeo4jDriver, *, database: str) -> None:
        self._driver = driver
        self.database = database
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(database={self.database!r}, closed={self._closed!r})"

    __str__ = __repr__

    @property
    def driver(self) -> AsyncNeo4jDriver:
        return self._driver

    async def verify_connectivity(self) -> None:
        await self._driver.verify_connectivity(database=self.database)

    async def close(self) -> None:
        if self._closed:
            return
        await self._driver.close()
        self._closed = True


def create_async_neo4j_driver(uri: str, auth: Neo4jAuth) -> AsyncNeo4jDriver:
    """Create an official Neo4j AsyncDriver without opening a connection."""

    driver: AsyncDriver = AsyncGraphDatabase.driver(uri, auth=auth)
    return cast(AsyncNeo4jDriver, driver)


def create_neo4j_resource_from_settings(
    settings: Settings,
    *,
    driver_factory: Neo4jDriverFactory = create_async_neo4j_driver,
) -> Neo4jResource:
    """Create the application-owned Neo4j resource from Settings.

    The password is extracted from ``SecretStr`` only at the driver boundary and
    is never stored in the resource representation.
    """

    driver = driver_factory(
        settings.neo4j_uri,
        (settings.neo4j_username, settings.neo4j_password.get_secret_value()),
    )
    return Neo4jResource(driver, database=settings.neo4j_database)
