from pathlib import Path
from typing import Annotated, NoReturn

import psycopg
import typer

from app.db import MigrationError, migrate_down, migrate_up
from app.settings import get_settings

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
def openapi() -> None:
    """Записать docs/openapi.json."""
    not_implemented("задача 5")


@app.command()
def seed() -> None:
    """Тестовые учётки и справочники."""
    not_implemented("BE-9")


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
) -> None:
    """Проверить возможности моделей."""
    not_implemented("ML-2")


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
