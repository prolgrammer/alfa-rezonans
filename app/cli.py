import asyncio
import json
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import psycopg
import typer

from app.db import MigrationError, close_pool, get_pool, migrate_down, migrate_up
from app.llm.errors import LLMNotConfigured
from app.settings import REPO_ROOT, get_settings

if TYPE_CHECKING:
    from app.llm.config import ModelsConfig
    from app.llm.probe import ProbeResult

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Ориентир: данные, сбор, агент, проверки")
llm_app = typer.Typer(no_args_is_help=True, help="Шлюз моделей")
sources_app = typer.Typer(no_args_is_help=True, help="Источники новостей")
app.add_typer(llm_app, name="llm")
app.add_typer(sources_app, name="sources")


def not_implemented(item: str) -> NoReturn:
    # код 2 отличает «команды ещё нет» от настоящей ошибки выполнения (1)
    typer.echo(f"ещё не реализовано ({item})", err=True)
    raise typer.Exit(2)


@app.command()
def migrate(
    direction: Annotated[str, typer.Argument(help="up или down")] = "up",
    steps: Annotated[int | None, typer.Argument(help="Сколько миграций откатить (только для down)")] = None,
) -> None:
    """Применить или откатить миграции."""
    if direction not in ("up", "down"):
        raise typer.BadParameter("ожидается up или down", param_hint="DIRECTION")
    if steps is not None and direction == "up":
        raise typer.BadParameter("число шагов задаётся только для down", param_hint="STEPS")

    settings = get_settings()
    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            if direction == "up":
                versions = migrate_up(conn, settings.migrations_dir)
                label = "Применены миграции"
            else:
                versions = migrate_down(conn, settings.migrations_dir, steps or 1)
                label = "Откатаны миграции"
    except (MigrationError, psycopg.Error) as exc:
        typer.echo(f"ошибка: {exc}", err=True)
        raise typer.Exit(1) from exc
    if versions:
        typer.echo(f"{label}: {', '.join(f'{v:04d}' for v in versions)}")
    else:
        typer.echo("Схема актуальна" if direction == "up" else "Откатывать нечего")


@app.command()
def openapi(
    check: Annotated[bool, typer.Option("--check", help="Только сверить файл, код 1 если устарел")] = False,
) -> None:
    """Записать docs/openapi.json."""
    # импорт здесь: создание приложения читает настройки, а остальным командам они не нужны
    from app.api.main import app as api_app

    target = REPO_ROOT / "docs" / "openapi.json"
    text = json.dumps(api_app.openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if check:
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            typer.echo("docs/openapi.json устарел: выполните `orientir openapi`", err=True)
            raise typer.Exit(1)
        typer.echo("docs/openapi.json актуален")
        return
    target.write_text(text, encoding="utf-8", newline="\n")
    typer.echo(f"Записан {target.relative_to(REPO_ROOT)}")


@app.command()
def seed() -> None:
    """Создать или обновить тестовые учётки из config/demo_accounts.yaml."""
    # импорт здесь: модуль читает каталог факторов, остальным командам он не нужен
    from app.api.accounts import AccountsError, demo_accounts, seed_accounts

    settings = get_settings()
    try:
        accounts = demo_accounts().accounts
        with psycopg.connect(settings.database_url) as conn:
            seed_accounts(conn, accounts, settings.demo_password.get_secret_value())
    except (AccountsError, psycopg.Error) as exc:
        typer.echo(f"ошибка: {exc}", err=True)
        raise typer.Exit(1) from exc
    header = ("логин", "компания", "роль", "права")
    rows = [header, *((a.login, a.company_id, a.user_role.value, a.access.value) for a in accounts)]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    typer.echo(f"Учёток: {len(rows) - 1}. Пароль — значение DEMO_PASSWORD")


@app.command("load-clients")
def load_clients(
    folder: Annotated[Path, typer.Argument(help="Папка с тремя CSV")],
    report: Annotated[bool, typer.Option("--report", help="Записать отчёт о данных")] = False,
) -> None:
    """Загрузить CSV клиентов."""
    not_implemented("DQ-1")


@app.command("ingest-bank")
def ingest_bank(folder: Annotated[Path, typer.Argument(help="Папка с PDF банка")]) -> None:
    """Загрузить аналитику банка."""
    not_implemented("BE-7")


@app.command()
def collect(
    source: Annotated[str | None, typer.Option("--source", help="Идентификатор источника")] = None,
    backfill: Annotated[bool, typer.Option("--backfill", help="Собрать историю")] = False,
) -> None:
    """Собрать новости."""
    not_implemented("BE-4")


@app.command("import")
def import_news(file: Annotated[Path, typer.Argument(help="Файл jsonl с новостями")]) -> None:
    """Импортировать новости из файла."""
    not_implemented("BE-8a")


@app.command()
def warm() -> None:
    """Прогреть разборы верха лент."""
    not_implemented("BE-10")


@app.command("eval")
def evaluate() -> None:
    """Посчитать метрики по эталону."""
    not_implemented("DQ-14")


@app.command()
def smoke() -> None:
    """Сквозная проверка сценария."""
    not_implemented("BE-13")


@llm_app.command("probe")
def llm_probe(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Только смета, без вызовов")] = False,
    model: Annotated[
        list[str] | None, typer.Option("--model", help="Проверить только эту модель (можно несколько раз)")
    ] = None,
) -> None:
    """Проверить возможности моделей и записать .models_caps.json."""
    # импорт здесь: шлюз читает конфиг моделей, остальным командам он не нужен
    from app.llm import gateway, probe
    from app.llm.config import load_config

    config = load_config(REPO_ROOT / "config" / "models.yaml")
    unknown = [m for m in model or [] if m not in config.models]
    if unknown:
        raise typer.BadParameter(f"нет в config/models.yaml: {', '.join(unknown)}", param_hint="--model")
    models = list(dict.fromkeys(model)) if model else list(config.models)

    if dry_run:
        rows = probe.estimate(config, models)
        for name, calls, rub in rows:
            typer.echo(f"{name:<28}{calls:>3} вызовов  до {rub:.2f} ₽")
        typer.echo(f"Итого: {sum(r[1] for r in rows)} вызовов, до {sum(r[2] for r in rows):.2f} ₽")
        return

    try:
        result = _run_async(_probe(config, models))
    except LLMNotConfigured as exc:
        typer.echo(f"ошибка: {exc}", err=True)
        raise typer.Exit(1) from exc

    if result.catalog is None:
        typer.echo("Каталог провайдера: недоступен")
    else:
        typer.echo(f"Каталог провайдера: {len(result.catalog)} моделей")
        for name, (found, family) in probe.catalog_matches(config, result.catalog).items():
            mark = "есть" if found else "нет"
            typer.echo(f"  {name:<28}URI {mark}; семейство: {', '.join(family) or '—'}")
    typer.echo("")
    for line in probe.render_table(result.reports):
        typer.echo(line)
    total = sum(r.cost_rub for r in result.reports)
    typer.echo(f"Потрачено: {total:.3f} ₽")
    # готовые отчёты пишем и при остановке: деньги на них уже потрачены
    if result.reports:
        probe.write_caps(gateway.CAPS_FILE, result)
        typer.echo(f"Записан {gateway.CAPS_FILE.name}")
    lines = probe.discrepancies(config, result)
    typer.echo("")
    typer.echo("Расхождения с config/models.yaml:" if lines else "Расхождений с config/models.yaml нет")
    for line in lines:
        typer.echo(f"  - {line}")
    if result.stopped:
        typer.echo(f"ошибка: прогон остановлен — {result.stopped}", err=True)
        raise typer.Exit(1)
    if result.reports and all(r.status == "unavailable" for r in result.reports):
        # так выглядит и неверный ключ у провайдера, который отвечает на него 403
        typer.echo("ошибка: ни одна модель не доступна — проверьте ключ, каталог и LLM_BASE_URL", err=True)
        raise typer.Exit(1)


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(coro, loop_factory=factory)


async def _probe(config: "ModelsConfig", models: list[str]) -> "ProbeResult":
    from app.llm.gateway import Gateway
    from app.llm.probe import run_probe
    from app.llm.store import MemoryStore, PgStore, Store

    Gateway(config).check_configured()
    # БД открываем заранее и ждём: в коротком процессе фоновое открытие не успело бы,
    # и строки llm_calls первых вызовов остались бы в памяти, мимо дневного учёта.
    # Без внешнего таймаута: отмена посреди открытия оставила бы пул с живыми воркерами.
    store: Store
    typer.echo("Подключаюсь к БД для учёта вызовов…")
    try:
        store = PgStore(await get_pool())
    except Exception as exc:
        typer.echo(f"БД недоступна ({type(exc).__name__}): вызовы probe не попадут в llm_calls", err=True)
        store = MemoryStore()
    gw: Gateway | None = None
    try:
        gw = Gateway(config, store=store)
        return await run_probe(gw, models, on_model=lambda r: typer.echo(f"  {r.name}: {r.status}"))
    finally:
        if gw is not None:
            await gw.aclose()
        await close_pool()


@sources_app.command("test")
def sources_test() -> None:
    """Проверить ленты и robots.txt."""
    not_implemented("BE-4")


@app.command()
def demo() -> None:
    """Загрузить демо-данные."""
    not_implemented("BE-12")


@app.command()
def status(watch: Annotated[bool, typer.Option("--watch", help="Обновлять на месте")] = False) -> None:
    """Показать, сколько собрано, разобрано, прогрето."""
    not_implemented("BE-10")


@app.command("lint-text")
def lint_text(paths: Annotated[list[Path], typer.Argument(help="Файлы и папки")]) -> None:
    """Проверить тексты линтером штампов."""
    not_implemented("ML-16")
