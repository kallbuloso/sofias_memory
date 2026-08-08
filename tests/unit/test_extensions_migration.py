from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_PATH = MIGRATIONS_VERSIONS / "0001_enable_required_extensions.py"
EXPECTED_EXTENSIONS = ("vector", "pg_trgm", "citext")


class OpSpy:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def load_migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_extensions_migration", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load extension migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_text() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def upgrade_statements() -> list[str]:
    module = load_migration_module()
    op_spy = OpSpy()
    module.op = op_spy

    module.upgrade()

    return op_spy.statements


def test_exactly_one_real_revision_exists() -> None:
    revision_files = sorted(path.name for path in MIGRATIONS_VERSIONS.glob("*.py"))

    assert revision_files == ["0001_enable_required_extensions.py"]


def test_initial_revision_metadata() -> None:
    module = load_migration_module()

    assert module.revision == "0001"
    assert module.down_revision is None
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_enables_vector() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS vector" in upgrade_statements()


def test_upgrade_enables_pg_trgm() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in upgrade_statements()


def test_upgrade_enables_citext() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS citext" in upgrade_statements()


def test_upgrade_uses_if_not_exists_for_every_extension() -> None:
    statements = upgrade_statements()

    assert len(statements) == len(EXPECTED_EXTENSIONS)
    assert all(statement.startswith("CREATE EXTENSION IF NOT EXISTS ") for statement in statements)


def test_migration_does_not_use_destructive_or_unrequested_extension_options() -> None:
    text = migration_text().upper()
    statements = " ".join(upgrade_statements()).upper()

    assert "CASCADE" not in text
    assert " VERSION " not in statements
    assert " SCHEMA " not in statements


def test_migration_does_not_create_additional_extensions() -> None:
    statements = upgrade_statements()

    assert statements == [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE EXTENSION IF NOT EXISTS citext",
    ]


def test_migration_does_not_create_tables() -> None:
    assert "CREATE TABLE" not in migration_text().upper()


def test_migration_does_not_create_postgresql_enums() -> None:
    text = migration_text().upper()

    assert "CREATE TYPE" not in text
    assert "ENUM" not in text


def test_migration_does_not_create_indexes() -> None:
    assert "CREATE INDEX" not in migration_text().upper()


def test_migration_does_not_define_vector_columns_yet() -> None:
    assert "VECTOR(3072)" not in migration_text().upper()


def test_migration_does_not_define_hnsw_yet() -> None:
    assert "HNSW" not in migration_text().upper()


def test_downgrade_does_not_drop_extensions() -> None:
    text = migration_text().upper()

    assert "DROP EXTENSION" not in text


def test_downgrade_policy_is_documented() -> None:
    text = migration_text()

    assert "Leave shared database capabilities installed." in text
    assert "cannot prove whether these extensions already" in text
    assert "could break managed database provisioning" in text


def test_gitkeep_does_not_remain_with_real_revision() -> None:
    assert not (MIGRATIONS_VERSIONS / ".gitkeep").exists()
