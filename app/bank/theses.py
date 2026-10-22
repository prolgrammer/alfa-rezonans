import argparse
import asyncio
import contextlib
import csv
import datetime as dt
import io
import json
import logging
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.catalogs import factor_ids, factor_names
from app.contracts import BankChunk, BankThesis, ThesisItem, ThesisNumber
from app.contracts.enums import FactorDirection, Horizon, ThesisKind
from app.db import close_pool, get_pool
from app.errors import NotFound
from app.fixtures import load_fixture
from app.llm import LLMUnavailable, gw
from app.text.sanitize import neutralize, sanitize, wrap_source
from app.text.verify import (
    QUOTE_MIN,
    NumberMark,
    clip,
    find_quote,
    normalize,
    number_marks,
    quote_size,
    show_number,
    unit_matches,
)

log = logging.getLogger(__name__)

PROMPT = "bank_theses"
THESES_MAX = 20
FACTORS_MAX = 5  # BankThesis.factors — до 5
# ~2 тыс. токенов на страницу и ~80 тыс. на документ: аналитическая записка банка укладывается целиком,
# а случайно загруженный годовой отчёт не съест суточный бюджет одним вызовом
PAGE_LIMIT = 6000
DOC_LIMIT = 240_000
_ERROR_TEXT = 60
# тезис в списке перегенерации: хватает, чтобы модель узнала свой ответ
_FAILED_TEXT = 900

_CHUNKS = TypeAdapter(list[BankChunk])
_THESES = TypeAdapter(list[ThesisItem])


# заглушка до BE-7 роли 3: фрагменты документа со страницами из разбора PDF
async def doc_chunks(doc_id: int) -> list[BankChunk]:
    return [c for c in load_fixture("bank_chunks.json", _CHUNKS) if c.doc_id == doc_id]


class _DraftNumber(BaseModel):
    what: str
    value: float
    unit: str


class _DraftThesis(BaseModel):
    text: str
    quote: str
    page: int
    kind: ThesisKind
    factors: list[str]
    direction: FactorDirection
    horizon: Horizon
    numbers: list[_DraftNumber]


class ThesesDraft(BaseModel):
    """Ответ модели: поля BankThesis без ограничений длины, факторы — строками.

    Длины и id проверяет код: иначе одна длинная строка или неверный id роняли бы весь ответ,
    а не один тезис. Закрытые списки — перечислениями: их держит строгая схема ответа.
    """

    theses: list[_DraftThesis]


@dataclass(frozen=True)
class _Checked:
    # тезис после проверки: None — не принят; иначе — без того, что не прошло
    thesis: BankThesis | None
    errors: tuple[str, ...]
    draft: _DraftThesis


@dataclass(frozen=True)
class BuiltTheses:
    theses: tuple[BankThesis, ...]
    retried: bool
    # что проверка удалила: тезисы целиком или их числа и факторы
    removed: tuple[str, ...]


def _show_mark(mark: NumberMark) -> str:
    value, scale, percent = mark
    return show_number(value) + (f" {scale}" if scale else "") + ("%" if percent else "")


def _check(draft: _DraftThesis, shown: Mapping[int, str], known: frozenset[str]) -> _Checked:
    """Проверка одного тезиса. Цитата, числа текста и хотя бы один фактор обязательны;
    лишние числа из numbers и неизвестные факторы удаляются из тезиса с ошибкой."""
    text = clip(draft.text, 500)
    label = f"тезис «{clip(draft.text, _ERROR_TEXT)}»"

    def rejected(problem: str) -> _Checked:
        return _Checked(None, (f"{label}: {problem}",), draft)

    if not text:
        return rejected("пустой текст")
    if quote_size(draft.quote) < QUOTE_MIN:
        return rejected(f"цитата короче {QUOTE_MIN} символов: приведи фразу со страницы целиком")
    found = find_quote(draft.quote, shown)
    if not found:
        return rejected(f"цитаты «{clip(draft.quote, _ERROR_TEXT)}» нет дословно ни на одной странице")
    # где стоит цитата, код знает точнее модели: интерфейс откроет именно эту страницу
    page = draft.page if draft.page in found else min(found)
    # числа сверяются с той цитатой, что попадёт в тезис: обрезка могла бы задеть число
    quote = clip(draft.quote, 600)
    in_quote = number_marks(quote)
    missing = number_marks(text) - in_quote
    if missing:
        listed = ", ".join(sorted(map(_show_mark, missing)))
        return rejected(f"чисел {listed} нет в цитате в том же виде (масштаб, %)")
    errors: list[str] = []
    factors: list[str] = []
    for factor in draft.factors:
        if factor not in known:
            errors.append(f"{label}: фактора «{clip(factor, _ERROR_TEXT)}» нет в каталоге")
        elif factor not in factors:
            factors.append(factor)
    if not factors:
        return _Checked(None, (*errors, f"{label}: нет ни одного фактора из каталога"), draft)
    accepted: list[ThesisNumber] = []
    for number in draft.numbers:
        what, unit = clip(number.what, 120), clip(number.unit, 20)
        value = abs(Decimal(repr(number.value)))
        if not what:
            errors.append(f"{label}: у числа {show_number(value)} пустое описание")
        # unit_matches ищет значение целым числом источника: «4%» не найдётся внутри «14%»
        elif not unit_matches(value, unit, quote):
            errors.append(f"{label}: значения {show_number(value)} {unit} нет в цитате")
        else:
            accepted.append(ThesisNumber(what=what, value=number.value, unit=unit))
    thesis = BankThesis(
        text=text,
        quote=quote,
        page=page,
        kind=draft.kind,
        factors=factors[:FACTORS_MAX],
        direction=draft.direction,
        horizon=draft.horizon,
        numbers=accepted[:10],
    )
    return _Checked(thesis, tuple(errors), draft)


def _key(thesis: BankThesis) -> tuple[tuple[int, str], str]:
    return (thesis.page, normalize(thesis.quote)), normalize(thesis.text)


class _Collected:
    """Принятые тезисы без повторов: та же страница и цитата или тот же текст — повтор."""

    def __init__(self) -> None:
        self.theses: list[BankThesis] = []
        self._quotes: set[tuple[int, str]] = set()
        self._texts: set[str] = set()

    def add(self, thesis: BankThesis) -> None:
        quote, text = _key(thesis)
        if quote in self._quotes or text in self._texts:
            return
        self._quotes.add(quote)
        self._texts.add(text)
        self.theses.append(thesis)


@dataclass
class _Prepared:
    variables: dict[str, Any]
    shown: dict[int, str]
    dropped_pages: list[int] = field(default_factory=list)


def _prepare(title: str, published_on: dt.date, chunks: Sequence[BankChunk]) -> _Prepared | None:
    pages: dict[int, list[str]] = {}
    for chunk in sorted(chunks, key=lambda c: (c.page, c.id)):
        if chunk.text.strip():
            pages.setdefault(chunk.page, []).append(chunk.text.strip())
    if not pages:
        return None
    blocks, total, dropped = [], 0, []
    for page, parts in pages.items():
        # номер источника — страница: страницы идут не подряд, поэтому не render_sources с нумерацией подряд
        block = wrap_source(page, "\n\n".join(parts), limit=PAGE_LIMIT)
        if total + len(block.text) > DOC_LIMIT:
            dropped.append(page)
            continue
        total += len(block.text) + 2
        blocks.append(block)
    variables = {
        "title": neutralize(sanitize(title)).text,
        "published_on": published_on.isoformat(),
        "count": len(blocks),
        "theses_max": THESES_MAX,
        "factors_catalog": list(factor_names().items()),
        "sources": "\n\n".join(block.text for block in blocks),
        "failed": [],
    }
    return _Prepared(variables, {int(block.n): block.body for block in blocks}, dropped)


def _failed_item(checked: _Checked) -> dict[str, str]:
    # в списке — ответ модели, а он мог повторить текст источника: обезвреживаем так же, как источник
    shown = json.dumps(checked.draft.model_dump(mode="json"), ensure_ascii=False)
    return {
        "thesis": neutralize(clip(shown, _FAILED_TEXT)).text,
        "errors": neutralize("; ".join(checked.errors)).text,
    }


async def build_theses(
    title: str, published_on: dt.date, chunks: Sequence[BankChunk], *, doc_id: int | None = None
) -> BuiltTheses | None:
    """Тезисы документа без БД. None — в документе нет текста; сбой LLM на первом вызове поднимается."""
    prepared = _prepare(title, published_on, chunks)
    if prepared is None:
        return None
    if prepared.dropped_pages:
        log.warning(
            "документ %s: страницы %s не влезли в предел %s символов",
            doc_id,
            prepared.dropped_pages,
            DOC_LIMIT,
        )
    known = factor_ids()
    draft = await gw.json(PROMPT, PROMPT, prepared.variables, ThesesDraft)
    first = [_check(d, prepared.shown, known) for d in draft.theses[:THESES_MAX]]
    collected = _Collected()
    for checked in first:
        if checked.thesis is not None and not checked.errors:
            collected.add(checked.thesis)
    failed = [c for c in first if c.errors]
    removed: list[str] = []
    if failed:
        # одна перегенерация — только провалившихся тезисов; принятые не пересылаются и не теряются
        try:
            retry = await gw.json(
                PROMPT,
                PROMPT,
                prepared.variables | {"failed": [_failed_item(c) for c in failed]},
                ThesesDraft,
            )
        except LLMUnavailable as error:
            log.info("документ %s: перегенерация тезисов не удалась, берём проверенное: %s", doc_id, error)
            removed.extend(e for c in failed for e in c.errors)
        else:
            # лишнее сверх числа провалившихся — не исправление, а новые тезисы без первой проверки
            second = [_check(d, prepared.shown, known) for d in retry.theses[: len(failed)]]
            for checked in second:
                if checked.thesis is not None:
                    collected.add(checked.thesis)
                removed.extend(checked.errors)
            if len(second) < len(failed):
                removed.append(
                    f"перегенерация вернула {len(second)} из {len(failed)} тезисов, остальные не исправлены"
                )
    for checked in failed:
        # без лишних чисел и неизвестных факторов тезис первого ответа проверен; модель могла его
        # пропустить при перегенерации или не ответить — тогда остаётся он
        if checked.thesis is not None:
            collected.add(checked.thesis)
    return BuiltTheses(tuple(collected.theses[:THESES_MAX]), retried=bool(failed), removed=tuple(removed))


# --- хранение ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Doc:
    title: str
    published_on: dt.date


@dataclass(frozen=True)
class _Stored:
    thesis: BankThesis
    verified: bool | None
    prompt_version: str


@dataclass
class _DocLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_doc_locks: dict[int, _DocLock] = {}


@contextlib.asynccontextmanager
async def _doc_lock(doc_id: int) -> AsyncIterator[None]:
    # один документ одновременно строится один раз: второй вызов дождётся первого и возьмёт его тезисы
    entry = _doc_locks.setdefault(doc_id, _DocLock())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        # замок нужен, пока документ строится или его ждут: иначе словарь рос бы весь век процесса
        entry.users -= 1
        if not entry.users:
            del _doc_locks[doc_id]


async def _doc(doc_id: int) -> _Doc:
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute("SELECT title, published_on FROM bank_docs WHERE id = %s", (doc_id,))
        row = await cursor.fetchone()
    if row is None:
        raise NotFound(f"документ банка {doc_id} не найден")
    return _Doc(row["title"], row["published_on"])


async def _stored(doc_id: int) -> list[_Stored] | None:
    """Сохранённые тезисы документа; None — какой-то не проходит контракт, и документ строится заново."""
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute(
            "SELECT payload, verified, prompt_version FROM bank_theses WHERE doc_id = %s ORDER BY id",
            (doc_id,),
        )
        rows = await cursor.fetchall()
    try:
        return [
            _Stored(BankThesis.model_validate(r["payload"]), r["verified"], r["prompt_version"]) for r in rows
        ]
    except ValidationError:
        log.warning("тезисы документа %s не проходят текущий контракт: строим заново", doc_id)
        return None


async def _replace(
    doc_id: int,
    theses: Sequence[BankThesis],
    published_on: dt.date,
    version: str,
    previous: Sequence[_Stored],
) -> None:
    # отметка вычитки переносится, только если человек проверял то же утверждение с той же цитатой
    marks = {_key(s.thesis): s.verified for s in previous if s.verified is not None}
    async with (await get_pool()).connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM bank_theses WHERE doc_id = %s", (doc_id,))
        for thesis in theses:
            await conn.execute(
                "INSERT INTO bank_theses (doc_id, page, payload, factors, kind, published_on, verified, "
                "prompt_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    doc_id,
                    thesis.page,
                    Jsonb(thesis.model_dump(mode="json")),
                    list(thesis.factors),
                    thesis.kind.value,
                    published_on,
                    marks.get(_key(thesis)),
                    version,
                ),
            )


async def extract_theses(doc_id: int, *, force: bool = False) -> list[BankThesis]:
    """Тезисы документа банка с проверкой кодом; повтор с той же версией промпта не идёт к модели.

    Сбой модели не роняет вызов: в БД ничего не пишется, возвращаются уже сохранённые тезисы.
    """
    async with _doc_lock(doc_id):
        doc = await _doc(doc_id)
        version = gw.prompt_version(PROMPT)
        stored = await _stored(doc_id)
        if not force and stored and all(s.prompt_version == version for s in stored):
            return [s.thesis for s in stored]
        previous = stored or []
        try:
            built = await build_theses(doc.title, doc.published_on, await doc_chunks(doc_id), doc_id=doc_id)
        except LLMUnavailable as error:
            log.warning("тезисы документа %s: модель недоступна (%s)", doc_id, error)
            return [s.thesis for s in previous]
        if built is None:
            log.warning("тезисы документа %s: в документе нет текста", doc_id)
            return [s.thesis for s in previous]
        if built.removed:
            log.info(
                "тезисы документа %s: проверка удалила %s: %s", doc_id, len(built.removed), built.removed
            )
        await _replace(doc_id, built.theses, doc.published_on, version, previous)
        return list(built.theses)


# --- позиция банка и вычитка -------------------------------------------------------------------------------

_ITEM_SQL = (
    "SELECT t.id, t.doc_id, left(d.title, 300) AS doc_title, t.published_on, t.verified, t.payload "
    "FROM bank_theses t JOIN bank_docs d ON d.id = t.doc_id "
)


def _items(rows: Sequence[Mapping[str, Any]]) -> list[ThesisItem]:
    items = []
    for row in rows:
        try:
            fields = {k: row[k] for k in ("id", "doc_id", "doc_title", "published_on", "verified")}
            items.append(ThesisItem.model_validate(fields | {"thesis": row["payload"]}))
        except ValidationError:
            log.warning("тезис %s не проходит текущий контракт: пропущен", row["id"])
    return items


async def get_bank_view(factor: str, limit: int = 3) -> list[ThesisItem]:
    """Вычитанные тезисы банка по фактору, свежие первыми."""
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute(
            _ITEM_SQL + "WHERE t.verified IS TRUE AND %s = ANY(t.factors) "
            "ORDER BY t.published_on DESC, t.id LIMIT %s",
            (factor, limit),
        )
        return _items(await cursor.fetchall())


async def fixture_bank_view(factor: str, limit: int = 3) -> list[ThesisItem]:
    """Позиция банка из фикстуры — для MOCK-режима API и тестов без БД."""
    rows = [
        t for t in load_fixture("theses.json", _THESES) if t.verified is True and factor in t.thesis.factors
    ]
    return sorted(rows, key=lambda t: t.published_on, reverse=True)[:limit]


async def list_theses(doc_id: int | None = None) -> list[ThesisItem]:
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute(
            _ITEM_SQL + "WHERE %(doc)s::bigint IS NULL OR t.doc_id = %(doc)s ORDER BY t.doc_id, t.page, t.id",
            {"doc": doc_id},
        )
        return _items(await cursor.fetchall())


async def set_verified(thesis_id: int, verified: bool | None) -> ThesisItem:
    async with (await get_pool()).connection() as conn:
        await conn.execute("UPDATE bank_theses SET verified = %s WHERE id = %s", (verified, thesis_id))
        cursor = await conn.execute(_ITEM_SQL + "WHERE t.id = %s", (thesis_id,))
        items = _items(await cursor.fetchall())
    if not items:
        raise NotFound(f"тезис {thesis_id} не найден")
    return items[0]


REVIEW_COLUMNS = ("id", "doc", "page", "kind", "factors", "direction", "text", "quote", "numbers", "ok")
_YES = frozenset({"1", "+", "да", "yes", "true", "верно", "ok"})
_NO = frozenset({"0", "-", "нет", "no", "false", "неверно"})


def _mark(verified: bool | None) -> str:
    return "" if verified is None else ("1" if verified else "0")


def _numbers_cell(numbers: Sequence[ThesisNumber]) -> str:
    return "; ".join(
        f"{n.what}: {show_number(abs(Decimal(repr(n.value))))} {n.unit}".strip() for n in numbers
    )


async def export_review(doc_id: int | None = None) -> tuple[str, int]:
    """CSV для вычитки командой: колонка ok — 1 (верно), 0 (неверно) или пусто.

    Разделитель «;» — чтобы Excel с русской локалью открыл файл без мастера импорта.
    """
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(REVIEW_COLUMNS)
    items = await list_theses(doc_id)
    for item in items:
        t = item.thesis
        writer.writerow(
            (
                item.id,
                item.doc_title,
                t.page,
                t.kind.value,
                ", ".join(t.factors),
                t.direction.value,
                t.text,
                t.quote,
                _numbers_cell(t.numbers),
                _mark(item.verified),
            )
        )
    return out.getvalue(), len(items)


@dataclass(frozen=True)
class ReviewResult:
    updated: int
    missing: tuple[int, ...]
    # строки с непонятной отметкой или номером: номер строки файла и что в ней
    bad: tuple[str, ...]


def _parse_review(text: str) -> tuple[dict[int, bool], list[str]]:
    header = text.partition("\n")[0]
    # Excel сохраняет CSV с «;» или «,» в зависимости от локали
    delimiter = ";" if header.count(";") >= header.count(",") else ","
    marks: dict[int, bool] = {}
    bad: list[str] = []
    for line, row in enumerate(csv.DictReader(io.StringIO(text), delimiter=delimiter), 2):
        ok = (row.get("ok") or "").strip().casefold()
        if not ok:
            continue
        try:
            thesis_id = int((row.get("id") or "").strip())
        except ValueError:
            bad.append(f"строка {line}: номер тезиса «{row.get('id')}»")
            continue
        if ok in _YES:
            marks[thesis_id] = True
        elif ok in _NO:
            marks[thesis_id] = False
        else:
            bad.append(f"строка {line}: отметка «{ok}»")
    return marks, bad


async def apply_review(text: str) -> ReviewResult:
    """Отметки из CSV вычитки; пустая отметка ничего не меняет. Всё пишется в одной транзакции."""
    marks, bad = _parse_review(text)
    missing: list[int] = []
    async with (await get_pool()).connection() as conn, conn.transaction():
        for thesis_id, verified in marks.items():
            cursor = await conn.execute(
                "UPDATE bank_theses SET verified = %s WHERE id = %s", (verified, thesis_id)
            )
            if not cursor.rowcount:
                # тезисы документа перестроены после выгрузки: номера старых больше нет
                missing.append(thesis_id)
    return ReviewResult(len(marks) - len(missing), tuple(missing), tuple(bad))


def read_review_file(path: Path) -> str:
    raw = path.read_bytes()
    # Excel с русской локалью сохраняет CSV в cp1251, а наша выгрузка — в UTF-8 с BOM
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


async def estimate_theses(doc_ids: Sequence[int]) -> list[tuple[int, float]]:
    """Верхняя оценка прогона в рублях по документам — один вызов основной модели, без перегенерации."""
    estimates = []
    for doc_id in doc_ids:
        doc = await _doc(doc_id)
        prepared = _prepare(doc.title, doc.published_on, await doc_chunks(doc_id))
        rub = 0.0 if prepared is None else gw.estimate_json(PROMPT, PROMPT, prepared.variables, ThesesDraft)
        estimates.append((doc_id, rub))
    return estimates


async def _main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.bank.theses", description="Вычитка тезисов банка")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="выгрузить тезисы в CSV с колонкой ok")
    export.add_argument("path", type=Path)
    export.add_argument("--doc", type=int, help="только этот документ")
    apply = commands.add_parser("apply", help="загрузить отметки ok из CSV")
    apply.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            text, count = await export_review(args.doc)
            args.path.write_text(text, encoding="utf-8-sig", newline="")
            print(f"выгружено тезисов: {count} → {args.path}")
            return 0
        result = await apply_review(read_review_file(args.path))
        print(f"отмечено: {result.updated}")
        if result.missing:
            print(f"нет в БД (документ перестроен после выгрузки): {', '.join(map(str, result.missing))}")
        for problem in result.bad:
            print(f"пропущено — {problem}")
        return 1 if result.missing or result.bad else 0
    finally:
        await close_pool()


if __name__ == "__main__":
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    sys.exit(asyncio.run(_main(sys.argv[1:]), loop_factory=factory))
