from __future__ import annotations

import configparser
from pathlib import Path

from sofias_memory.infrastructure.postgres import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
ENV_PY = MIGRATIONS_DIR / "env.py"
SCRIPT_TEMPLATE = MIGRATIONS_DIR / "script.py.mako"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def alembic_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI, encoding="utf-8")
    return parser


def test_alembic_ini_points_to_migrations_directory() -> None:
    config = alembic_config()

    assert config.get("alembic", "script_location", raw=True) == "%(here)s/migrations"


def test_alembic_ini_uses_project_root_on_python_path() -> None:
    config = alembic_config()

    assert config.get("alembic", "prepend_sys_path", raw=True) == "%(here)s"


def test_alembic_paths_do_not_contain_developer_absolute_path() -> None:
    content = read_text(ALEMBIC_INI) + read_text(ENV_PY)

    assert "D:\\Applications" not in content
    assert "C:\\Users" not in content


def test_target_metadata_is_official_base_metadata() -> None:
    env_content = read_text(ENV_PY)

    assert "from sofias_memory.infrastructure.postgres import (" in env_content
    assert "Base," in env_content
    assert "target_metadata = Base.metadata" in env_content
    assert Base.metadata.naming_convention is not None


def test_migration_environment_uses_settings_for_database_url() -> None:
    env_content = read_text(ENV_PY)

    assert "load_settings()" in env_content
    assert "settings.database_url.get_secret_value()" in env_content
    assert "os.getenv" not in env_content
    assert "set_main_option" not in env_content


def test_alembic_ini_does_not_contain_database_url_or_secret() -> None:
    content = read_text(ALEMBIC_INI)

    assert "sqlalchemy.url" not in content
    assert "postgresql+asyncpg://" not in content
    assert "db-secret" not in content
    assert "change-me" not in content


def test_env_py_does_not_create_global_engine() -> None:
    env_content = read_text(ENV_PY)

    assert "engine = create_async_engine_from_settings(settings)" in env_content
    assert "create_async_engine_from_settings(load_settings())" not in env_content
    assert "asyncio.run(run_async_migrations_online())" in env_content


def test_importing_runtime_postgres_infrastructure_does_not_connect() -> None:
    import sofias_memory.infrastructure.postgres as postgres

    assert postgres.Base.metadata is Base.metadata


def test_versions_directory_exists() -> None:
    assert VERSIONS_DIR.is_dir()


def test_versions_directory_contains_expected_foundation_revisions() -> None:
    revision_files = sorted(path.name for path in VERSIONS_DIR.glob("*.py"))

    assert revision_files == [
        "0001_enable_required_extensions.py",
        "0002_create_datasets.py",
    ]


def test_revision_template_contains_upgrade_and_downgrade() -> None:
    template = read_text(SCRIPT_TEMPLATE)

    assert "def upgrade() -> None:" in template
    assert "def downgrade() -> None:" in template


def test_revision_template_contains_alembic_revision_fields() -> None:
    template = read_text(SCRIPT_TEMPLATE)

    assert "revision: str = ${repr(up_revision)}" in template
    assert "down_revision: str | Sequence[str] | None = ${repr(down_revision)}" in template
    assert "branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}" in template
    assert "depends_on: str | Sequence[str] | None = ${repr(depends_on)}" in template


def test_migration_environment_is_prepared_for_async() -> None:
    env_content = read_text(ENV_PY)

    assert "async def run_async_migrations_online() -> None:" in env_content
    assert "async with engine.connect() as connection:" in env_content
    assert "await connection.run_sync(run_migrations_with_connection)" in env_content


def test_compare_type_and_server_default_are_enabled() -> None:
    env_content = read_text(ENV_PY)

    assert "compare_type=True" in env_content
    assert "compare_server_default=True" in env_content


def test_base_naming_convention_is_not_recreated_in_alembic_env() -> None:
    env_content = read_text(ENV_PY)

    assert "MetaData(" not in env_content
    assert "NAMING_CONVENTION" not in env_content
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


def test_no_sqlite_fallback_was_introduced() -> None:
    content = read_text(ALEMBIC_INI) + read_text(ENV_PY) + read_text(SCRIPT_TEMPLATE)

    assert "sqlite" not in content.lower()


def test_migrations_root_gitkeep_removed() -> None:
    assert not (MIGRATIONS_DIR / ".gitkeep").exists()
