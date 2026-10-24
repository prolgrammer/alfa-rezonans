import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.api.errors import ApiError
from app.api.passwords import hash_password, needs_rehash, verify_password
from app.api.schemas import DemoSwitch
from app.contracts import Access, SessionCtx, UserRole
from app.db import get_pool
from app.settings import Settings

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15


@dataclass
class AuthSession:
    """Сессия в том виде, в каком её видят маршруты: одинаково для MOCK и для БД."""

    ctx: SessionCtx
    login: str
    csrf_token: str


def hash_token(token: str) -> str:
    # В БД только хеш: утечка таблицы sessions не даёт войти под чужим токеном.
    return hashlib.sha256(token.encode()).hexdigest()


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "Нужно войти")


async def _audit(conn: AsyncConnection[Any], user_id: int | None, action: str, details: dict) -> None:
    await conn.execute(
        "INSERT INTO audit_log (user_id, action, details) VALUES (%s, %s, %s)",
        (user_id, action, Jsonb(details)),
    )


def _session_from_row(row: dict[str, Any]) -> AuthSession:
    access = Access(row["access"])
    # Выбор клиента действует, только пока у учётки есть право выбирать: после понижения прав сессия
    # возвращается к своей компании.
    acting = access in (Access.expert, Access.admin)
    company_id = (acting and row["acting_company_id"]) or row["company_id"]
    if company_id is None:
        raise ApiError(403, "forbidden", "Учётке не назначена компания")
    ctx = SessionCtx(
        user_id=row["user_id"],
        company_id=company_id,
        user_role=UserRole((acting and row["acting_user_role"]) or row["user_role"]),
        access=access,
    )
    return AuthSession(ctx=ctx, login=row["login"], csrf_token=row["csrf_token"])


_UNLOCKED = "(locked_until IS NULL OR locked_until <= now())"
# argon2id берёт 64 МиБ на проверку: без ограничения поток входов съел бы память процесса.
_HASH_SLOTS = asyncio.Semaphore(4)


async def _verify(password_hash: str | None, password: str) -> bool:
    async with _HASH_SLOTS:
        return await asyncio.to_thread(verify_password, password_hash, password)


_SESSION_SELECT = (
    "SELECT s.csrf_token, s.acting_company_id, s.acting_user_role, s.expires_at <= now() AS expired, "
    "u.id AS user_id, u.login, u.company_id, u.user_role, u.access "
    "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s"
)


class DbAuth:
    """Сессии боевого режима: токен в cookie, в БД его SHA-256."""

    def __init__(self, settings: Settings) -> None:
        self.ttl_hours = settings.session_ttl_hours

    async def current(self, token: str | None) -> AuthSession:
        if not token:
            raise _unauthorized()
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(_SESSION_SELECT, (hash_token(token),))
            row = await cur.fetchone()
            if row is not None and row["expired"]:
                await conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))
        # Исключения — после выхода из блока: иначе откат отменил бы удаление истёкшей строки.
        if row is None or row["expired"]:
            raise _unauthorized()
        return _session_from_row(row)

    async def login(self, login: str, password: str, old_token: str | None) -> tuple[AuthSession, str]:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE expires_at <= now()")
            cur = await conn.execute(
                "SELECT id, login, password_hash, company_id, user_role, access, "
                "locked_until > now() AS locked, "
                "CEIL(EXTRACT(EPOCH FROM (locked_until - now())) / 60)::int AS lock_minutes "
                "FROM users WHERE login = %s",
                (login,),
            )
            user = await cur.fetchone()
            if user is not None and user["locked"]:
                await _audit(conn, user["id"], "login_locked", {"login": login, "reason": "still_locked"})
        if user is not None and user["locked"]:
            raise self._locked_error(user["lock_minutes"])
        # Хеш считается без соединения: иначе десяток входов подряд занял бы весь пул.
        valid = await _verify(user["password_hash"] if user else None, password)
        async with pool.connection() as conn:
            outcome = await self._finish(conn, user, login, password, valid, old_token)
        # Исключения — после коммита: счётчик неудач и журнал не должны откатываться вместе с ответом.
        if isinstance(outcome, ApiError):
            raise outcome
        return outcome

    async def _finish(
        self,
        conn: AsyncConnection[Any],
        user: dict[str, Any] | None,
        login: str,
        password: str,
        valid: bool,
        old_token: str | None,
    ) -> tuple[AuthSession, str] | ApiError:
        wrong = ApiError(401, "unauthorized", "Неверный логин или пароль")
        if user is None:
            # Введённый логин не пишем: в поле логина нередко по ошибке набирают пароль.
            await _audit(conn, None, "login_failed", {"reason": "unknown_login"})
            return wrong
        # Обе записи — только если учётку не заблокировал параллельный запрос, пока считался хеш:
        # иначе пачка одновременных попыток обходила бы лимит, а верный пароль снимал бы блокировку.
        if not valid:
            cur = await conn.execute(
                "UPDATE users SET "
                "failed_logins = CASE WHEN failed_logins + 1 >= %s THEN 0 ELSE failed_logins + 1 END, "
                "locked_until = CASE WHEN failed_logins + 1 >= %s "
                "THEN now() + make_interval(mins => %s) ELSE locked_until END "
                f"WHERE id = %s AND {_UNLOCKED} RETURNING failed_logins = 0 AS just_locked",
                (MAX_FAILED_LOGINS, MAX_FAILED_LOGINS, LOCK_MINUTES, user["id"]),
            )
            updated = await cur.fetchone()
            if updated is None:
                return await self._locked_now(conn, user["id"])
            if updated["just_locked"]:
                await _audit(
                    conn, user["id"], "login_locked", {"login": login, "reason": "too_many_failures"}
                )
                return self._locked_error(LOCK_MINUTES)
            await _audit(conn, user["id"], "login_failed", {"login": login, "reason": "wrong_password"})
            return wrong
        cur = await conn.execute(
            f"UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = %s AND {_UNLOCKED} "
            "RETURNING id",
            (user["id"],),
        )
        if await cur.fetchone() is None:
            return await self._locked_now(conn, user["id"])
        if needs_rehash(user["password_hash"]):
            new_hash = await asyncio.to_thread(hash_password, password)
            await conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user["id"]))
        if user["company_id"] is None:
            return ApiError(403, "forbidden", "Учётке не назначена компания")
        # Старую сессию снимаем только при успешном входе: неудачная попытка не должна выбрасывать из системы.
        if old_token:
            await conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(old_token),))
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        await conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(hours => %s))",
            (hash_token(token), user["id"], csrf, self.ttl_hours),
        )
        await _audit(conn, user["id"], "login_ok", {"login": login})
        ctx = SessionCtx(
            user_id=user["id"],
            company_id=user["company_id"],
            user_role=UserRole(user["user_role"]),
            access=Access(user["access"]),
        )
        return AuthSession(ctx=ctx, login=user["login"], csrf_token=csrf), token

    async def _locked_now(self, conn: AsyncConnection[Any], user_id: int) -> ApiError:
        cur = await conn.execute(
            "SELECT GREATEST(CEIL(EXTRACT(EPOCH FROM (locked_until - now())) / 60)::int, 1) AS minutes "
            "FROM users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
        await _audit(conn, user_id, "login_locked", {"reason": "still_locked"})
        return self._locked_error(row["minutes"] if row and row["minutes"] else LOCK_MINUTES)

    @staticmethod
    def _locked_error(minutes: int) -> ApiError:
        return ApiError(
            429,
            "rate_limited",
            f"Слишком много неверных паролей. Повторите через {minutes} мин.",
            {"retry_after_minutes": minutes},
        )

    async def logout(self, token: str | None, session: AuthSession) -> None:
        if not token:
            return
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))
            await _audit(conn, session.ctx.user_id, "logout", {"login": session.login})

    async def switch(self, token: str | None, session: AuthSession, choice: DemoSwitch) -> AuthSession:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE sessions SET acting_company_id = %s, acting_user_role = %s "
                "WHERE token_hash = %s RETURNING 1 AS ok",
                (choice.company_id, choice.user_role.value, hash_token(token or "")),
            )
            if await cur.fetchone() is None:
                raise _unauthorized()
            await _audit(
                conn,
                session.ctx.user_id,
                "demo_switch",
                {"company_id": choice.company_id, "user_role": choice.user_role.value},
            )
        ctx = session.ctx.model_copy(update={"company_id": choice.company_id, "user_role": choice.user_role})
        return AuthSession(ctx=ctx, login=session.login, csrf_token=session.csrf_token)
