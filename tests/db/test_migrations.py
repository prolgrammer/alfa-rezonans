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


def copy_migrations(target: Path, *versions: int) -> None:
    for version in versions:
        for path in MIGRATIONS.glob(f"{version:04d}_*.sql"):
            shutil.copy(path, target / path.name)


def copy_first_migration(target: Path) -> None:
    copy_migrations(target, 1)


def test_up_down_up(db_conn):
    assert migrate_up(db_conn, MIGRATIONS) == [1, 2, 3]
    assert migrate_up(db_conn, MIGRATIONS) == []
    assert applied(db_conn) == [1, 2, 3]

    assert migrate_down(db_conn, MIGRATIONS, 3) == [3, 2, 1]
    assert public_tables(db_conn) == {"schema_migrations"}
    assert applied(db_conn) == []

    assert migrate_up(db_conn, MIGRATIONS) == [1, 2, 3]


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
    assert [m.version for m in discover(MIGRATIONS)] == [1, 2, 3]


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


def event_id_column(conn):
    return conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'llm_calls' AND column_name = 'event_id'"
    ).fetchone()


def test_llm_calls_event_id_column(db_conn):
    migrate_up(db_conn, MIGRATIONS)
    assert event_id_column(db_conn) == ("YES",)

    assert migrate_down(db_conn, MIGRATIONS, 2) == [3, 2]
    assert event_id_column(db_conn) is None


CLIENT_COLUMNS = [
    ("companies", "mainokved_name"),
    ("companies", "amount_workers_fns_gradation"),
    ("companies", "amount_workers_spark_gradation"),
    ("company_persons", "city_name"),
]


def column_type(conn, table: str, column: str):
    row = conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row[0] if row else None


def trace_indexes(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'agent_traces'"
    )
    return dict(rows.fetchall())


def test_client_columns_and_trace_key(db_conn):
    migrate_up(db_conn, MIGRATIONS)
    assert [column_type(db_conn, t, c) for t, c in CLIENT_COLUMNS] == ["text"] * 4
    indexes = trace_indexes(db_conn)
    assert "agent_traces_insight_idx" not in indexes
    assert "UNIQUE" in indexes["agent_traces_insight_id_seq_key"]

    assert migrate_down(db_conn, MIGRATIONS, 1) == [3]
    assert [column_type(db_conn, t, c) for t, c in CLIENT_COLUMNS] == [None] * 4
    assert set(trace_indexes(db_conn)) == {
        "agent_traces_pkey",
        "agent_traces_insight_idx",
        "agent_traces_chat_idx",
    }


def test_trace_key_keeps_last_duplicate(db_conn, tmp_path):
    copy_migrations(tmp_path, 1, 2)
    migrate_up(db_conn, tmp_path)
    event_id = db_conn.execute("INSERT INTO events (title) VALUES ('e') RETURNING id").fetchone()[0]
    insight_id = db_conn.execute(
        "INSERT INTO insights (event_id, company_id, user_role, cache_key) "
        "VALUES (%s, 'C', 'cfo', 'k') RETURNING id",
        (event_id,),
    ).fetchone()[0]
    chat = "6f1f1d9e-0000-4000-8000-000000000001"
    insert = "INSERT INTO agent_traces (insight_id, chat_id, seq, step) VALUES (%s, %s, %s, %s::jsonb)"
    for seq, step in ((1, '{"n": "old"}'), (1, '{"n": "new"}'), (2, '{"n": "only"}')):
        db_conn.execute(insert, (insight_id, None, seq, step))
    # у чата insight_id пуст, ключ его строк не касается
    for _ in range(2):
        db_conn.execute(insert, (None, chat, 1, "{}"))

    copy_migrations(tmp_path, 3)
    assert migrate_up(db_conn, tmp_path) == [3]
    rows = db_conn.execute(
        "SELECT seq, step->>'n' FROM agent_traces WHERE insight_id = %s ORDER BY seq", (insight_id,)
    ).fetchall()
    assert rows == [(1, "new"), (2, "only")]
    assert db_conn.execute("SELECT count(*) FROM agent_traces WHERE chat_id IS NOT NULL").fetchone()[0] == 2
