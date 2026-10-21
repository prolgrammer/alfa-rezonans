import re

import psycopg
import pytest

from app.contracts import enums
from app.db import migrate_up
from app.settings import get_settings

UP_SQL = get_settings().migrations_dir / "0001_init.up.sql"

SPEC_TABLES = {
    "companies", "company_persons", "payments", "company_profiles", "group_profiles", "profile_overrides",
    "users", "sessions", "sources", "raw_items", "events", "event_items", "market_series", "bank_docs",
    "bank_chunks", "bank_theses", "relevance", "insights", "agent_traces", "tool_calls", "llm_calls",
    "llm_cache", "feedback", "expert_reviews", "chat_messages", "audit_log",
}  # fmt: skip

ENUM_COLUMNS = [
    ("raw_items", "status", enums.RawItemStatus),
    ("events", "card_status", enums.CardStatus),
    ("sources", "kind", enums.SourceKind),
    ("sources", "type", enums.SourceType),
    ("bank_theses", "kind", enums.ThesisKind),
    ("relevance", "user_role", enums.UserRole),
    ("relevance", "stage", enums.RelevanceStage),
    ("insights", "status", enums.InsightStatus),
    ("insights", "mode", enums.InsightMode),
    ("insights", "user_role", enums.UserRole),
    ("users", "user_role", enums.UserRole),
    ("users", "access", enums.Access),
    ("sessions", "acting_user_role", enums.UserRole),
    ("feedback", "verdict", enums.FeedbackVerdict),
    ("feedback", "reason", enums.FeedbackReason),
    ("expert_reviews", "action", enums.ExpertAction),
    ("chat_messages", "author", enums.ChatAuthor),
    ("profile_overrides", "origin", enums.OverrideOrigin),
]


@pytest.fixture
def migrated(db_conn: psycopg.Connection) -> psycopg.Connection:
    migrate_up(db_conn, get_settings().migrations_dir)
    return db_conn


def test_every_table_has_writer_comment():
    lines = UP_SQL.read_text(encoding="utf-8").splitlines()
    tables = [i for i, line in enumerate(lines) if line.startswith("CREATE TABLE ")]
    assert len(tables) == len(SPEC_TABLES)
    for index in tables:
        above = []
        j = index - 1
        while j >= 0 and lines[j].startswith("--"):
            above.append(lines[j])
            j -= 1
        assert any(line.startswith("-- пишет:") for line in above), lines[index]


def test_all_spec_tables_exist(migrated):
    rows = migrated.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    assert {row[0] for row in rows} - {"schema_migrations"} == SPEC_TABLES
    assert len(SPEC_TABLES) == 26


@pytest.mark.parametrize(("table", "column", "enum"), ENUM_COLUMNS, ids=lambda v: getattr(v, "__name__", v))
def test_enum_checks_match_contract(migrated, table, column, enum):
    rows = migrated.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
        (table, f"{table}_{column}_check"),
    ).fetchall()
    assert len(rows) == 1, f"нет CHECK {table}.{column}"
    assert set(re.findall(r"'([^']+)'", rows[0][0])) == {member.value for member in enum}


def test_trace_rows_need_an_owner(migrated):
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated.execute("INSERT INTO agent_traces (seq, step) VALUES (1, '{}')")
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated.execute(
            "INSERT INTO tool_calls (user_id, tool, args, result_chars, duration_ms, ok) "
            "VALUES (1, 'get_bank_view', '{}', 0, 0, true)"
        )


def test_range_checks(migrated):
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated.execute(
            "INSERT INTO sources (id, name, type, kind, url, trust, interval_min) "
            "VALUES ('s', 'S', 'rss', 'news', 'https://example.test', 1.5, 10)"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated.execute(
            "INSERT INTO group_profiles (group_id, version, hash, completeness, features, exposures) "
            "VALUES ('g', 1, 'h', 2, '[]', '[]')"
        )


def test_russian_full_text_search_and_unique_url(migrated):
    migrated.execute(
        "INSERT INTO sources (id, name, type, kind, url, trust, interval_min) "
        "VALUES ('s', 'S', 'rss', 'news', 'https://example.test', 0.9, 10)"
    )
    insert = (
        "INSERT INTO raw_items (source_id, url, canonical_url, title, published_at, content_hash) "
        "VALUES ('s', 'https://example.test/a', 'https://example.test/a', %s, now(), 'h')"
    )
    migrated.execute(insert, ("Банк России сохранил ключевую ставку",))
    hit = migrated.execute(
        "SELECT count(*) FROM raw_items WHERE tsv @@ plainto_tsquery('russian', 'ключевой ставки')"
    ).fetchone()
    assert hit[0] == 1
    with pytest.raises(psycopg.errors.UniqueViolation):
        migrated.execute(insert, ("Дубль",))


def test_vector_columns_have_expected_dimension(migrated):
    rows = migrated.execute(
        "SELECT c.relname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid WHERE a.attname = 'embedding' AND c.relkind = 'r'"
    ).fetchall()
    assert dict(rows) == {"events": "vector(384)", "bank_chunks": "vector(384)"}


def test_tool_calls_keep_error_text(migrated):
    columns = {
        row[0]
        for row in migrated.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tool_calls'"
        ).fetchall()
    }
    assert "error" in columns


@pytest.mark.parametrize("table", ["feedback", "expert_reviews"])
def test_insight_id_is_a_foreign_key(migrated, table):
    rows = migrated.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f'",
        (table,),
    ).fetchall()
    assert any("(insight_id)" in r[0] and "REFERENCES insights(id)" in r[0] for r in rows)
