"""SQLAlchemy declarative base shared by future PostgreSQL models."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for future ORM models.

    SQLAlchemy reserves the Python attribute ``metadata`` on declarative bases.
    Future ORM models with a SQL column named ``metadata`` must map it with a
    different Python attribute, for example ``metadata_ = mapped_column("metadata", ...)``.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
