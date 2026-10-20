import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.settings import get_settings

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Path
    down: Path


def discover(directory: Path) -> list[Migration]:
    found: dict[int, dict[str, Path]] = {}
    names: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILE.match(path.name)
        if not match:
            raise MigrationError(f"файл {path.name} не похож на NNNN_имя.up.sql / NNNN_имя.down.sql")
        version, name, direction = int(match[1]), match[2], match[3]
        if names.setdefault(version, name) != name:
            raise MigrationError(
                f"версия {version:04d} встречается с разными именами: {names[version]}, {name}"
            )
        found.setdefault(version, {})[direction] = path
    result = []
    for version in sorted(found):
        pair = found[version]
        for direction in ("up", "down"):
            if direction not in pair:
                raise MigrationError(f"у миграции {version:04d}_{names[version]} нет файла .{direction}.sql")
        result.append(Migration(version, names[version], pair["up"], pair["down"]))
    return result


def _prepare(conn: psycopg.Connection) -> None:
    # Каждая миграция — своя транзакция через conn.transaction(); в обычном режиме
    # первый же запрос открыл бы внешнюю транзакцию, и откат одной миграции стал бы невозможен.
    if not conn.autocommit:
        raise MigrationError("нужно соединение в режиме autocommit")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def applied(conn: psycopg.Connection) -> list[int]:
    _prepare(conn)
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate_up(conn: psycopg.Connection, directory: Path) -> list[int]:
    migrations = discover(directory)
    done = set(applied(conn))
    known = {m.version for m in migrations}
    if done - known:
        raise MigrationError(f"в базе есть версии без файлов: {sorted(done - known)}")
    pending = [m for m in migrations if m.version not in done]
    if pending and done and pending[0].version < max(done):
        raise MigrationError(
            f"пропущена версия {pending[0].version:04d}: она старше применённой {max(done):04d}"
        )
    result = []
    for migration in pending:
        try:
            with conn.transaction():
                conn.execute(migration.up.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (migration.version, migration.name),
                )
        except psycopg.Error as exc:
            raise MigrationError(f"миграция {migration.version:04d}_{migration.name}: {exc}") from exc
        result.append(migration.version)
    return result


def migrate_down(conn: psycopg.Connection, directory: Path, steps: int = 1) -> list[int]:
    if steps < 1:
        raise MigrationError("число шагов отката должно быть не меньше 1")
    by_version = {m.version: m for m in discover(directory)}
    result = []
    for version in sorted(applied(conn), reverse=True)[:steps]:
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"для применённой версии {version:04d} нет файлов миграции")
        try:
            with conn.transaction():
                conn.execute(migration.down.read_text(encoding="utf-8"))
                conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        except psycopg.Error as exc:
            raise MigrationError(f"откат {version:04d}_{migration.name}: {exc}") from exc
        result.append(version)
    return result


_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        pool = AsyncConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        _pool = pool
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
