import json

import httpx
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import app.api.sessions as sessions_module
from app.api.main import create_app
from app.api.passwords import hash_password
from app.api.sessions import hash_token
from app.db import migrate_up
from app.settings import get_settings

PASSWORD = "correct-horse"
CSRF = "X-CSRF-Token"


class StubBackend:
    """Вход не читает данные клиентов, ему нужны только имя компании и проверка её существования."""

    async def company_name(self, ctx) -> str:
        return f"Компания {ctx.company_id}"

    async def has_company(self, company: str) -> bool:
        return company in {"C-DEV-01", "C-IT-01"}


@pytest.fixture
async def db(db_conn, test_db_url, monkeypatch):
    migrate_up(db_conn, get_settings().migrations_dir)
    password_hash = hash_password(PASSWORD)
    for login, access in (("anna", "client"), ("expert", "expert")):
        db_conn.execute(
            "INSERT INTO users (login, password_hash, company_id, user_role, access) "
            "VALUES (%s, %s, 'C-DEV-01', 'cfo', %s)",
            (login, password_hash, access),
        )
    pool = AsyncConnectionPool(test_db_url, kwargs={"row_factory": dict_row}, open=False)
    await pool.open()

    async def get_pool():
        return pool

    monkeypatch.setattr(sessions_module, "get_pool", get_pool)
    yield db_conn
    await pool.close()


def live_client(make_settings, scheme="http"):
    app = create_app(make_settings(mock=False))
    app.state.backend = StubBackend()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://test")


@pytest.fixture
async def client(db, make_settings):
    async with live_client(make_settings) as c:
        yield c


async def login(client, name="anna", password=PASSWORD):
    return await client.post("/api/auth/login", json={"login": name, "password": password})


def user_row(db, name):
    return db.execute("SELECT failed_logins, locked_until FROM users WHERE login = %s", (name,)).fetchone()


def actions(db):
    return [r[0] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]


async def test_login_gives_session_and_me(client, db):
    response = await login(client)
    assert response.status_code == 200
    body = response.json()
    assert (body["login"], body["company_id"], body["user_role"], body["access"]) == (
        "anna",
        "C-DEV-01",
        "cfo",
        "client",
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=43200" in cookie
    assert "secure" not in cookie
    me = await client.get("/api/me")
    assert me.status_code == 200
    assert me.json() == body
    # в БД лежит только хеш токена, а не он сам
    token = client.cookies["orientir_session"]
    stored = db.execute("SELECT token_hash, csrf_token FROM sessions").fetchall()
    assert stored == [(hash_token(token), body["csrf_token"])]


async def test_login_over_https_sets_secure_cookie(db, make_settings):
    async with live_client(make_settings, "https") as client:
        response = await login(client)
    assert "secure" in response.headers["set-cookie"].lower()


async def test_login_replaces_old_session(client, db):
    await login(client)
    first = client.cookies["orientir_session"]
    await login(client)
    assert client.cookies["orientir_session"] != first
    hashes = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
    assert hashes == [hash_token(client.cookies["orientir_session"])]


async def test_login_removes_expired_sessions(client, db):
    await login(client, "expert")
    db.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
    client.cookies.clear()
    await login(client)
    users = [r[0] for r in db.execute("SELECT u.login FROM sessions s JOIN users u ON u.id = s.user_id")]
    assert users == ["anna"]


async def test_wrong_password_is_unauthorized(client, db):
    response = await login(client, password="wrong")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Неверный логин или пароль"
    assert "set-cookie" not in response.headers
    assert user_row(db, "anna")[0] == 1


async def test_unknown_login_gets_same_answer(client, db):
    response = await login(client, "nobody")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Неверный логин или пароль"
    assert actions(db) == ["login_failed"]


async def test_fifth_wrong_password_locks_account(client, db):
    for _ in range(4):
        assert (await login(client, password="wrong")).status_code == 401
    response = await login(client, password="wrong")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    failed, locked_until = user_row(db, "anna")
    assert failed == 0
    assert locked_until is not None
    remaining = db.execute("SELECT locked_until - now() FROM users WHERE login = 'anna'").fetchone()[0]
    assert 14 * 60 < remaining.total_seconds() <= 15 * 60
    assert "15" in response.json()["error"]["message"]


async def test_correct_password_during_lock_is_429(client, db):
    for _ in range(5):
        await login(client, password="wrong")
    response = await login(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert "set-cookie" not in response.headers
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


async def test_login_works_after_lock_expires(client, db):
    for _ in range(5):
        await login(client, password="wrong")
    db.execute("UPDATE users SET locked_until = now() - interval '1 minute' WHERE login = 'anna'")
    assert (await login(client)).status_code == 200
    assert user_row(db, "anna") == (0, None)


async def test_success_resets_failed_counter(client, db):
    for _ in range(3):
        await login(client, password="wrong")
    assert user_row(db, "anna")[0] == 3
    assert (await login(client)).status_code == 200
    assert user_row(db, "anna") == (0, None)
    for _ in range(4):
        assert (await login(client, password="wrong")).status_code == 401


async def test_lock_is_per_account(client, db):
    for _ in range(5):
        await login(client, password="wrong")
    assert (await login(client, "expert")).status_code == 200


async def test_expired_session_is_unauthorized_and_removed(client, db):
    await login(client)
    db.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Нужно войти"
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


async def test_missing_or_forged_cookie_is_unauthorized(client):
    assert (await client.get("/api/me")).status_code == 401
    client.cookies.set("orientir_session", "forged-token")
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("headers", [{}, {CSRF: "wrong"}])
async def test_mutation_without_valid_csrf_is_forbidden(client, headers):
    await login(client)
    response = await client.post("/api/auth/logout", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert (await client.get("/api/me")).status_code == 200


async def test_logout_removes_session(client, db):
    csrf = (await login(client)).json()["csrf_token"]
    token = client.cookies["orientir_session"]
    response = await client.post("/api/auth/logout", headers={CSRF: csrf})
    assert response.status_code == 200
    assert "orientir_session=" in response.headers["set-cookie"]
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    # прежний токен не действует, даже если клиент его сохранил
    client.cookies.set("orientir_session", token)
    assert (await client.get("/api/me")).status_code == 401


async def test_expert_switches_company_and_role(client, db):
    csrf = (await login(client, "expert")).json()["csrf_token"]
    response = await client.post(
        "/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "treasurer"}, headers={CSRF: csrf}
    )
    assert response.status_code == 200
    me = (await client.get("/api/me")).json()
    assert (me["company_id"], me["user_role"], me["access"]) == ("C-IT-01", "treasurer", "expert")
    assert me["company_name"] == "Компания C-IT-01"
    assert db.execute("SELECT acting_company_id, acting_user_role FROM sessions").fetchone() == (
        "C-IT-01",
        "treasurer",
    )
    # учётка сама не изменилась: новый вход начинается со своей компании
    assert db.execute("SELECT company_id, user_role FROM users WHERE login = 'expert'").fetchone() == (
        "C-DEV-01",
        "cfo",
    )
    again = (await login(client, "expert")).json()
    assert (again["company_id"], again["user_role"]) == ("C-DEV-01", "cfo")


async def test_switch_to_unknown_company_is_404(client):
    csrf = (await login(client, "expert")).json()["csrf_token"]
    response = await client.post(
        "/api/demo/switch", json={"company_id": "C-XX", "user_role": "cfo"}, headers={CSRF: csrf}
    )
    assert response.status_code == 404
    assert (await client.get("/api/me")).json()["company_id"] == "C-DEV-01"


async def test_client_cannot_switch(client):
    csrf = (await login(client)).json()["csrf_token"]
    response = await client.post(
        "/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"}, headers={CSRF: csrf}
    )
    assert response.status_code == 403
    assert (await client.get("/api/me")).json()["company_id"] == "C-DEV-01"


async def test_switch_needs_csrf(client):
    await login(client, "expert")
    response = await client.post("/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"})
    assert response.status_code == 403


async def test_audit_log_records_events_without_passwords(client, db):
    await login(client, "nobody", "secret-one")
    await login(client, password="secret-two")
    for _ in range(4):
        await login(client, password="secret-three")
    db.execute("UPDATE users SET locked_until = NULL WHERE login = 'anna'")
    csrf = (await login(client)).json()["csrf_token"]
    await client.post("/api/auth/logout", headers={CSRF: csrf})
    csrf = (await login(client, "expert")).json()["csrf_token"]
    await client.post(
        "/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"}, headers={CSRF: csrf}
    )
    assert {"login_ok", "login_failed", "login_locked", "logout", "demo_switch"} <= set(actions(db))
    details = json.dumps([r[0] for r in db.execute("SELECT details FROM audit_log")])
    assert "secret" not in details
    assert PASSWORD not in details
    switch = db.execute("SELECT user_id, details FROM audit_log WHERE action = 'demo_switch'").fetchone()
    expert_id = db.execute("SELECT id FROM users WHERE login = 'expert'").fetchone()[0]
    assert switch == (expert_id, {"company_id": "C-IT-01", "user_role": "cfo"})
