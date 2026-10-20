import shutil
from pathlib import Path

import pytest

from app.db import MigrationError, applied, discover, migrate_down, migrate_up
from app.settings import get_settings

MIGRATIONS = get_settings().migrations_dir


def public_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return {row[0] for row in rows}


def write_pair(directory: Path, version: int, name: str, up: str, down: str) -> None:
    (directory / f"{version:04d}_{name}.up.sql").write_text(up, encoding="utf-8")
    (directory / f"{version:04d}_{name}.down.sql").write_text(down, encoding="utf-8")


def copy_first_migration(target: Path) -> None:
    for path in MIGRATIONS.glob("0001_*.sql"):
        shutil.copy(path, target / path.name)


def test_up_down_up(db_conn):
    assert migrate_up(db_conn, MIGRATIONS) == [1]
    assert migrate_up(db_conn, MIGRATIONS) == []
    assert applied(db_conn) == [1]

    assert migrate_down(db_conn, MIGRATIONS, 1) == [1]
    assert public_tables(db_conn) == {"schema_migrations"}
    assert applied(db_conn) == []

    assert migrate_up(db_conn, MIGRATIONS) == [1]


def test_failed_migration_rolls_back(db_conn, tmp_path):
    copy_first_migration(tmp_path)
    write_pair(
        tmp_path,
        2,
        "broken",
        "CREATE TABLE partial_table (id INT); SELECT 1 / 0;",
        "DROP TABLE partial_table;",
    )

    with pytest.raises(MigrationError, match="0002_broken"):
        migrate_up(db_conn, tmp_path)

    assert applied(db_conn) == [1]
    assert "partial_table" not in public_tables(db_conn)


def test_down_rolls_back_only_requested_steps(db_conn, tmp_path):
    copy_first_migration(tmp_path)
    write_pair(tmp_path, 2, "extra", "CREATE TABLE extra_table (id INT);", "DROP TABLE extra_table;")

    assert migrate_up(db_conn, tmp_path) == [1, 2]
    assert migrate_down(db_conn, tmp_path, 1) == [2]
    tables = public_tables(db_conn)
    assert "extra_table" not in tables
    assert "users" in tables
    assert applied(db_conn) == [1]


def test_discover_real_directory():
    assert [m.version for m in discover(MIGRATIONS)] == [1]


def test_discover_rejects_missing_down(tmp_path):
    (tmp_path / "0001_init.up.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"нет файла \.down\.sql"):
        discover(tmp_path)


def test_discover_rejects_missing_up(tmp_path):
    (tmp_path / "0001_init.down.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"нет файла \.up\.sql"):
        discover(tmp_path)


def test_discover_rejects_duplicate_version(tmp_path):
    write_pair(tmp_path, 1, "one", "SELECT 1;", "SELECT 1;")
    write_pair(tmp_path, 1, "other", "SELECT 1;", "SELECT 1;")
    with pytest.raises(MigrationError, match="с разными именами"):
        discover(tmp_path)


def test_discover_rejects_misnamed_file(tmp_path):
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="не похож"):
        discover(tmp_path)


def test_discover_orders_by_version(tmp_path):
    write_pair(tmp_path, 10, "ten", "SELECT 1;", "SELECT 1;")
    write_pair(tmp_path, 2, "two", "SELECT 1;", "SELECT 1;")
    assert [m.version for m in discover(tmp_path)] == [2, 10]
