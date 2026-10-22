import asyncio
import copy
import datetime as dt
from typing import Any

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter

from app.bank import theses
from app.bank.theses import (
    apply_review,
    estimate_theses,
    export_review,
    extract_theses,
    get_bank_view,
    list_theses,
    read_review_file,
    set_verified,
)
from app.contracts import BankChunk
from app.db import migrate_up
from app.errors import NotFound
from app.fixtures import load_fixture
from app.llm import AllModelsFailed
from app.settings import get_settings

CHUNKS = load_fixture("bank_chunks.json", TypeAdapter(list[BankChunk]))
RATE = {
    "text": "Банк ждёт снижения ставки не раньше начала 2027 года.",
    "quote": "снижение ключевой ставки возможно не раньше начала 2027 года",
    "page": 3,
    "kind": "forecast",
    "factors": ["key_rate", "inflation"],
    "direction": "flat",
    "horizon": "months",
    "numbers": [],
}
HOLD = {
    "text": "ЦБ сохранил ставку на 14,0% и ужесточил риторику.",
    "quote": "ставка без изменения на 14,0%, риторика стала жестче",
    "page": 1,
    "kind": "assessment",
    "factors": ["key_rate"],
    "direction": "flat",
    "horizon": "months",
    "numbers": [{"what": "ключевая ставка", "value": 14.0, "unit": "%"}],
}
CNY = {
    "text": "Курс юаня в IV квартале — 11,5–12,5 ₽.",
    "quote": "курс юаня в IV квартале ожидаем в диапазоне 11,5–12,5 ₽",
    "page": 1,
    "kind": "forecast",
    "factors": ["cny_rub"],
    "direction": "volatile",
    "horizon": "months",
    "numbers": [],
}


def answer(*items: dict[str, Any]) -> dict[str, Any]:
    return {"theses": [copy.deepcopy(i) for i in items]}


@pytest.fixture
async def db(db_conn, test_db_url, monkeypatch):
    migrate_up(db_conn, get_settings().migrations_dir)
    docs = {c.doc_id: c for c in CHUNKS}
    for doc_id, c in sorted(docs.items()):
        db_conn.execute(
            "INSERT INTO bank_docs (id, title, published_on, file_sha256, pages) OVERRIDING SYSTEM VALUE "
            "VALUES (%s, %s, %s, %s, 5)",
            (doc_id, c.doc_title, c.published_on, f"sha{doc_id}"),
        )
    pool = AsyncConnectionPool(test_db_url, kwargs={"row_factory": dict_row}, open=False)
    await pool.open()

    async def get_pool():
        return pool

    monkeypatch.setattr(theses, "get_pool", get_pool)
    yield pool
    await pool.close()


async def rows(pool, doc_id: int = 1) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT * FROM bank_theses WHERE doc_id = %s ORDER BY id", (doc_id,))
        return await cursor.fetchall()


async def test_theses_are_saved_unverified_and_served_from_db(db, fake):
    gateway = fake(answer(HOLD, RATE))
    first = await extract_theses(1)
    assert [t.page for t in first] == [1, 3]
    saved = await rows(db)
    assert [r["verified"] for r in saved] == [None, None]
    assert {r["prompt_version"] for r in saved} == {"bank_theses@1"}
    assert [r["factors"] for r in saved] == [["key_rate"], ["key_rate", "inflation"]]
    assert [r["kind"] for r in saved] == ["assessment", "forecast"]
    assert {r["published_on"] for r in saved} == {dt.date(2026, 9, 12)}
    assert saved[0]["payload"]["quote"] == HOLD["quote"]
    # повтор с той же версией промпта к модели не идёт
    assert await extract_theses(1) == first
    assert len(gateway.prompts) == 1


async def test_force_and_new_prompt_version_rebuild(db, fake):
    fake(answer(HOLD))
    await extract_theses(1)
    gateway = fake(answer(HOLD, RATE))
    assert len(await extract_theses(1, force=True)) == 2
    gateway = fake(answer(RATE), version="bank_theses@2")
    assert [t.page for t in await extract_theses(1)] == [3]
    assert [r["prompt_version"] for r in await rows(db)] == ["bank_theses@2"]
    assert len(gateway.prompts) == 1


async def test_review_mark_survives_rebuild_only_for_same_thesis(db, fake):
    fake(answer(HOLD, RATE))
    await extract_theses(1)
    hold_id, rate_id = (r["id"] for r in await rows(db))
    await set_verified(hold_id, True)
    await set_verified(rate_id, False)
    changed = RATE | {"text": "Снижение ставки — не раньше начала 2027 года."}
    fake(answer(HOLD, changed))
    await extract_theses(1, force=True)
    assert [r["verified"] for r in await rows(db)] == [True, None]


async def test_llm_failure_keeps_previous_theses(db, fake):
    fake(answer(HOLD))
    await extract_theses(1)
    fake(AllModelsFailed("нет"))
    kept = await extract_theses(1, force=True)
    assert [t.page for t in kept] == [1]
    assert len(await rows(db)) == 1


async def test_llm_failure_without_theses_is_empty(db, fake):
    fake(AllModelsFailed("нет"))
    assert await extract_theses(1) == []
    assert await rows(db) == []


async def test_unknown_document_is_not_found(db, fake):
    fake()
    with pytest.raises(NotFound):
        await extract_theses(999)


async def test_document_without_text_writes_nothing(db, fake, monkeypatch):
    async def no_chunks(doc_id):
        return []

    monkeypatch.setattr(theses, "doc_chunks", no_chunks)
    gateway = fake()
    assert await extract_theses(1) == []
    assert gateway.prompts == [] and await rows(db) == []


async def test_stored_thesis_failing_contract_is_rebuilt(db, fake):
    fake(answer(HOLD))
    await extract_theses(1)
    async with db.connection() as conn:
        await conn.execute("UPDATE bank_theses SET payload = payload - 'kind'")
    gateway = fake(answer(HOLD))
    assert len(await extract_theses(1)) == 1
    assert len(gateway.prompts) == 1


async def test_concurrent_calls_build_once(db, fake):
    gateway = fake(answer(HOLD))
    first, second = await asyncio.gather(extract_theses(1), extract_theses(1))
    assert first == second and len(gateway.prompts) == 1
    assert theses._doc_locks == {}


async def test_bank_view_is_verified_fresh_and_limited(db, fake):
    fake(answer(HOLD, RATE))
    await extract_theses(1)
    fake(
        answer(
            RATE
            | {
                "text": "Ставка снизится до 13,0% к концу 2026 года.",
                "quote": "ожидаем снижения ключевой ставки до 13,0% к концу 2026 года",
                "page": 2,
                "factors": ["key_rate"],
                "direction": "down",
            }
        )
    )
    await extract_theses(2)
    assert await get_bank_view("key_rate") == []
    for item in await list_theses():
        await set_verified(item.id, True)
    hold_id = (await rows(db))[0]["id"]
    await set_verified(hold_id, False)
    view = await get_bank_view("key_rate")
    # документ 1 от 12.09 свежее документа 2 от 14.08; непроверенный тезис не попадает
    assert [(v.doc_id, v.thesis.page) for v in view] == [(1, 3), (2, 2)]
    assert view[0].doc_title.startswith("Ключевая ставка ЦБ РФ")
    assert view[0].published_on == dt.date(2026, 9, 12) and view[0].verified is True
    assert [v.thesis.page for v in await get_bank_view("inflation")] == [3]
    assert len(await get_bank_view("key_rate", limit=1)) == 1
    assert await get_bank_view("cny_rub") == []


async def test_list_and_set_verified(db, fake):
    fake(answer(HOLD, RATE))
    await extract_theses(1)
    fake(answer(CNY))
    await extract_theses(4)
    assert [(i.doc_id, i.thesis.page) for i in await list_theses()] == [(1, 1), (1, 3), (4, 1)]
    assert [i.doc_id for i in await list_theses(4)] == [4]
    item = (await list_theses(4))[0]
    assert (await set_verified(item.id, True)).verified is True
    assert (await set_verified(item.id, None)).verified is None
    with pytest.raises(NotFound):
        await set_verified(10_000, True)


async def test_review_csv_round_trip(db, fake, tmp_path):
    fake(answer(HOLD, RATE | {"text": "=HYPERLINK(2027) " + RATE["text"]}))
    await extract_theses(1)
    fake(answer(CNY))
    await extract_theses(4)
    text, count = await export_review()
    # формула из текста модели в Excel не исполнится
    assert ";'=HYPERLINK(2027) Банк ждёт" in text
    assert count == 3
    lines = text.splitlines()
    assert lines[0] == "id;doc;page;kind;factors;direction;text;quote;numbers;ok"
    assert "ключевая ставка: 14 %" in text
    hold_id, rate_id = (r["id"] for r in await rows(db))
    cny_id = (await rows(db, 4))[0]["id"]
    edited = "\n".join(
        [
            "id;doc;ok",
            f"{hold_id};x;да",
            f"{rate_id};x;0",
            f"{cny_id};x;",
            "9999;x;1",
            "abc;x;1",
            f"{cny_id};x;может быть",
        ]
    )
    result = await apply_review(edited)
    assert result.updated == 2
    assert result.missing == (9999,)
    assert len(result.bad) == 2 and "строка 6" in result.bad[0] and "может быть" in result.bad[1]
    assert {i.id: i.verified for i in await list_theses()} == {hold_id: True, rate_id: False, cny_id: None}
    # Excel с русской локалью сохраняет в cp1251 и с запятыми
    path = tmp_path / "review.csv"
    path.write_bytes(f"id,doc,ok\n{cny_id},Валютный рынок,нет\n".encode("cp1251"))
    assert (await apply_review(read_review_file(path))).updated == 1
    assert (await list_theses(4))[0].verified is False


async def test_estimate_without_calls(db):
    estimates = dict(await estimate_theses([1, 4]))
    assert set(estimates) == {1, 4}
    # промпт с каталогом факторов и 6000 токенов ответа по цене DeepSeek, дважды: вызов и перегенерация
    assert all(5 < rub < 12 for rub in estimates.values())
    assert await rows(db) == []


async def test_empty_answer_does_not_erase_previous(db, fake):
    fake(answer(HOLD, RATE))
    first = await extract_theses(1)
    fake(answer())
    assert await extract_theses(1, force=True) == first
    assert len(await rows(db)) == 2


async def test_verified_thesis_missing_from_new_set_is_kept(db, fake):
    fake(answer(HOLD, RATE))
    await extract_theses(1)
    hold_id, _ = (r["id"] for r in await rows(db))
    await set_verified(hold_id, True)
    changed = RATE | {"text": "Снижение ставки — не раньше начала 2027 года."}
    fake(answer(changed), version="bank_theses@2")
    result = await extract_theses(1, force=True)
    assert len(result) == 2
    saved = await rows(db)
    # прежний RATE не вычитан и ушёл; вычитанный HOLD остался с новой версией
    assert [(r["payload"]["quote"], r["verified"]) for r in saved] == [
        (RATE["quote"], None),
        (HOLD["quote"], True),
    ]
    assert {r["prompt_version"] for r in saved} == {"bank_theses@2"}
