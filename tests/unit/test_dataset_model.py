from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres import Base, Dataset


def column_names() -> list[str]:
    return [column.name for column in Dataset.__table__.columns]


def unique_constraint_columns() -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Dataset.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_constraint_names() -> set[str | None]:
    return {
        constraint.name
        for constraint in Dataset.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def check_constraint_sql() -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Dataset.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_dataset_table_name() -> None:
    assert Dataset.__tablename__ == "datasets"


def test_dataset_has_expected_columns_only() -> None:
    assert column_names() == [
        "id",
        "name",
        "slug",
        "description",
        "status",
        "active_generation",
        "created_at",
        "updated_at",
    ]


def test_dataset_id_is_uuid_primary_key() -> None:
    column = Dataset.__table__.c.id

    assert isinstance(column.type, PostgreSQLUUID)
    assert column.type.as_uuid is True
    assert column.primary_key is True
    assert column.nullable is False
    assert Dataset().id is None
    assert Dataset.__mapper__.columns["id"].default is not None


def test_dataset_name_uses_citext_and_is_required_unique() -> None:
    column = Dataset.__table__.c.name

    assert isinstance(column.type, CITEXT)
    assert column.nullable is False
    assert ("name",) in unique_constraint_columns()


def test_dataset_slug_is_required_unique_text() -> None:
    column = Dataset.__table__.c.slug

    assert column.type.python_type is str
    assert column.nullable is False
    assert ("slug",) in unique_constraint_columns()


def test_dataset_description_is_nullable_text() -> None:
    column = Dataset.__table__.c.description

    assert column.type.python_type is str
    assert column.nullable is True


def test_dataset_status_uses_named_enum_with_exact_values() -> None:
    column = Dataset.__table__.c.status

    assert isinstance(column.type, ENUM)
    assert column.type.name == "dataset_status"
    assert column.type.enums == ["active", "deleting", "deleted"]
    assert column.nullable is False
    assert str(column.server_default.arg) == "active"


def test_dataset_status_python_type_is_domain_enum() -> None:
    assert Dataset.__annotations__["status"] == "Mapped[DatasetStatus]"
    assert DatasetStatus.ACTIVE.value == "active"


def test_dataset_active_generation_default_is_zero() -> None:
    column = Dataset.__table__.c.active_generation

    assert column.type.python_type is int
    assert column.nullable is False
    assert str(column.server_default.arg) == "0"


def test_dataset_timestamps_are_timezone_aware_and_required() -> None:
    for column_name in ("created_at", "updated_at"):
        column = Dataset.__table__.c[column_name]

        assert column.type.python_type.__name__ == "datetime"
        assert column.type.timezone is True
        assert column.nullable is False
        assert str(column.server_default.arg) == "now()"


def test_dataset_name_and_slug_reject_blank_values_structurally() -> None:
    assert {"ck_datasets_name_not_blank", "ck_datasets_slug_not_blank"} <= check_constraint_names()


def test_dataset_name_has_max_length_constraint_without_losing_citext() -> None:
    column = Dataset.__table__.c.name
    checks = check_constraint_sql()

    assert isinstance(column.type, CITEXT)
    assert checks["ck_datasets_name_max_length"] == "char_length(name::text) <= 120"


def test_dataset_has_no_metadata_column_or_reserved_attribute_mapping() -> None:
    assert "metadata" not in Dataset.__table__.c
    assert "metadata_" not in Dataset.__mapper__.attrs


def test_dataset_has_no_forbidden_ownership_or_soft_delete_columns() -> None:
    forbidden = {
        "tenant_id",
        "owner_id",
        "user_id",
        "organization_id",
        "deleted_at",
        "configuration",
    }

    assert forbidden.isdisjoint(column_names())


def test_base_metadata_contains_only_dataset_product_table_at_this_stage() -> None:
    assert set(Base.metadata.tables) == {"datasets", "documents", "sources"}


def test_dataset_can_be_constructed_without_mutating_settings_or_connecting() -> None:
    dataset = Dataset(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        name="Main",
        slug="main",
        status=DatasetStatus.ACTIVE,
    )

    assert dataset.name == "Main"
    assert dataset.slug == "main"
    assert dataset.status is DatasetStatus.ACTIVE
