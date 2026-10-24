from functools import lru_cache
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.api.passwords import hash_password
from app.catalogs import factor_ids
from app.contracts import Access, UserRole
from app.settings import get_settings


class AccountsError(Exception):
    pass


class DemoAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str
    company_id: str
    user_role: UserRole
    access: Access
    interests: list[str] = []

    @field_validator("login", "company_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("не может быть пустым")
        return value

    @field_validator("interests")
    @classmethod
    def _known_factors(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - factor_ids())
        if unknown:
            raise ValueError(f"неизвестные факторы: {', '.join(unknown)}")
        return value


class DemoAccounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companies: dict[str, str]
    accounts: list[DemoAccount]


def _describe(loc: tuple[int | str, ...], data: object) -> str:
    # номер в списке мало что говорит, поэтому учётку называем по логину из файла
    if len(loc) > 1 and loc[0] == "accounts" and isinstance(loc[1], int) and isinstance(data, dict):
        accounts = data.get("accounts")
        raw = accounts[loc[1]] if isinstance(accounts, list) and loc[1] < len(accounts) else None
        login = raw.get("login") if isinstance(raw, dict) else None
        if login:
            return f"учётка {login!r}: {'.'.join(map(str, loc[2:])) or 'запись'}"
    return ".".join(map(str, loc))


def load_accounts(path: Path) -> DemoAccounts:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AccountsError(f"{path.name}: {exc}") from exc
    try:
        result = DemoAccounts.model_validate(data)
    except ValidationError as exc:
        problems = [f"{_describe(err['loc'], data)}: {err['msg']}" for err in exc.errors()]
        raise AccountsError(f"{path.name}: " + "; ".join(problems)) from exc
    logins = [a.login for a in result.accounts]
    duplicates = sorted({x for x in logins if logins.count(x) > 1})
    if duplicates:
        raise AccountsError(f"{path.name}: повторяются логины: {', '.join(duplicates)}")
    return result


@lru_cache
def demo_accounts() -> DemoAccounts:
    return load_accounts(get_settings().config_dir / "demo_accounts.yaml")


def company_names() -> dict[str, str]:
    return demo_accounts().companies


def company_ids() -> frozenset[str]:
    accounts = demo_accounts()
    return frozenset(accounts.companies) | {a.company_id for a in accounts.accounts}


def seed_accounts(conn: psycopg.Connection, accounts: list[DemoAccount], password: str) -> None:
    """Создаёт или обновляет учётки одной транзакцией; сессии пересеянных удаляет."""
    # argon2 намеренно медленный, а пароль у всех учёток общий, поэтому хеш считается один раз
    password_hash = hash_password(password)
    with conn.transaction():
        ids: list[int] = []
        for account in accounts:
            row = conn.execute(
                "INSERT INTO users (login, password_hash, company_id, user_role, access, interests) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (login) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, company_id = EXCLUDED.company_id, "
                "user_role = EXCLUDED.user_role, access = EXCLUDED.access, interests = EXCLUDED.interests, "
                "failed_logins = 0, locked_until = NULL "
                "RETURNING id",
                (
                    account.login,
                    password_hash,
                    account.company_id,
                    account.user_role.value,
                    account.access.value,
                    Jsonb(account.interests),
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
        # после смены пароля старые cookie не должны действовать
        conn.execute("DELETE FROM sessions WHERE user_id = ANY(%s)", (ids,))
