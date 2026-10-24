import pytest
from typer.testing import CliRunner

from app.api.accounts import DemoAccount, demo_accounts, seed_accounts
from app.api.passwords import verify_password
from app.cli import app
from app.db import migrate_up
from app.settings import get_settings

PASSWORD = "seed-test-password"


@pytest.fixture
def conn(db_conn):
    migrate_up(db_conn, get_settings().migrations_dir)
    return db_conn


def account(login="u1", **kw):
    values = {"company_id": "C-1", "user_role": "cfo", "access": "client", "interests": ["key_rate"]}
    return DemoAccount(login=login, **{**values, **kw})


def user(conn, login):
    return conn.execute(
        "SELECT id, password_hash, company_id, user_role, access, interests, failed_logins, locked_until "
        "FROM users WHERE login = %s",
        (login,),
    ).fetchone()


def add_session(conn, user_id, token):
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) "
        "VALUES (%s, %s, 'c', now() + interval '1 hour')",
        (token, user_id),
    )


def test_creates_accounts(conn):
    seed_accounts(
        conn, [account("u1"), account("u2", user_role="owner", access="expert", interests=[])], PASSWORD
    )
    row = user(conn, "u1")
    assert row[2:6] == ("C-1", "cfo", "client", ["key_rate"])
    assert row[1].startswith("$argon2id$")
    assert verify_password(row[1], PASSWORD)
    assert not verify_password(row[1], "other")
    assert user(conn, "u2")[3:5] == ("owner", "expert")


def test_idempotent_and_resets_lock(conn):
    seed_accounts(conn, [account()], PASSWORD)
    first_id = user(conn, "u1")[0]
    conn.execute(
        "UPDATE users SET failed_logins = 4, locked_until = now() + interval '1 hour', company_id = 'X'"
    )
    seed_accounts(conn, [account()], "new-password")
    row = user(conn, "u1")
    assert row[0] == first_id
    assert (row[2], row[6], row[7]) == ("C-1", 0, None)
    assert verify_password(row[1], "new-password")
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_drops_sessions_of_reseeded_only(conn):
    seed_accounts(conn, [account("u1"), account("stranger")], PASSWORD)
    add_session(conn, user(conn, "u1")[0], "t1")
    add_session(conn, user(conn, "stranger")[0], "t2")
    seed_accounts(conn, [account("u1")], PASSWORD)
    tokens = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert tokens == ["t2"]


def test_leaves_unlisted_account_untouched(conn):
    seed_accounts(conn, [account("stranger", company_id="C-OLD")], PASSWORD)
    conn.execute("UPDATE users SET failed_logins = 3 WHERE login = 'stranger'")
    before = user(conn, "stranger")
    seed_accounts(conn, [account("u1")], PASSWORD)
    assert user(conn, "stranger") == before


def test_seed_command(test_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        assert runner.invoke(app, ["migrate"]).exit_code == 0
        result = runner.invoke(app, ["seed"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    expected = demo_accounts().accounts
    for item in expected:
        assert item.login in result.output
    assert PASSWORD not in result.output
