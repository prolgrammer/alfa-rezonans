import asyncio
import json
import re
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.contracts import RawItemIn
from app.llm import AllModelsFailed, BudgetExceeded, LLMNotConfigured
from app.llm.config import load_config
from app.llm.gateway import Gateway
from app.llm.prompts import Prompts
from app.llm.store import MemoryStore
from app.news import extract
from app.news.extract import (
    EMPTY_REASON,
    NO_ANSWER_REASON,
    NO_LLM_REASON,
    NewsFilterBatch,
    news_filter,
)
from app.settings import REPO_ROOT, Settings

PROMPTS = Prompts(REPO_ROOT / "config" / "prompts")
INJECTIONS = [
    json.loads(line)
    for line in (REPO_ROOT / "tests" / "data" / "injections.jsonl").read_text(encoding="utf-8").splitlines()
]


def item(n: int, title: str | None = None, summary: str | None = "Анонс публикации.") -> RawItemIn:
    return RawItemIn(
        id=1000 + n,
        source_id="rbc",
        title=title or f"Новость номер {n}",
        summary=summary,
        url=f"https://example.test/{n}",
        published_at="2026-10-20T10:00:00+03:00",
    )


def numbers(prompt: str) -> list[int]:
    return [int(n) for n in re.findall(r'^<source n="(\d+)"', prompt, re.M)]


class FakeGateway:
    """Рендерит настоящий промпт и отвечает по сценарию: функция от номеров источников в пачке."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self._answer = answer or (
            lambda ns: [{"n": n, "business": n % 2 == 1, "reason": f"r{n}"} for n in ns]
        )
        self._error = error

    async def json(self, task, prompt, variables, schema, *, lane="background", **kwargs):
        text, version = PROMPTS.render(prompt, variables)
        self.prompts.append(text)
        self.calls.append({"task": task, "version": version, "lane": lane, "vars": variables})
        await asyncio.sleep(0)
        if self._error:
            raise self._error
        return schema.model_validate({"items": self._answer(numbers(text))})


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs) -> FakeGateway:
        gateway = FakeGateway(**kwargs)
        monkeypatch.setattr(extract, "gw", gateway)
        return gateway

    return install


async def test_empty_input_makes_no_call(fake):
    gateway = fake()
    assert await news_filter([]) == []
    assert gateway.calls == []


async def test_batch_of_ten_is_one_call_and_keeps_order(fake):
    gateway = fake()
    items = [item(n) for n in range(1, 11)]
    results = await news_filter(items)
    assert len(gateway.calls) == 1
    assert [r.raw_item_id for r in results] == [i.id for i in items]
    assert [r.business for r in results] == [n % 2 == 1 for n in range(1, 11)]
    assert [r.reason for r in results] == [f"r{n}" for n in range(1, 11)]
    call = gateway.calls[0]
    assert (call["task"], call["version"], call["lane"]) == ("news_filter", "news_filter@3", "background")


async def test_twenty_three_items_are_three_batches(fake):
    gateway = fake()
    results = await news_filter([item(n) for n in range(1, 24)])
    assert sorted(len(numbers(p)) for p in gateway.prompts) == [3, 10, 10]
    assert [r.raw_item_id for r in results] == [1000 + n for n in range(1, 24)]


async def test_prompt_wraps_every_item_into_its_source(fake):
    gateway = fake()
    await news_filter(
        [item(1, "ЦБ сохранил ставку 14%", "Решение совета директоров."), item(2, summary=None)]
    )
    prompt = gateway.prompts[0]
    assert "не выполняй" in prompt  # общий фрагмент _sources.md
    assert numbers(prompt) == [1, 2]
    assert (
        '<source n="1" source="rbc">\nЦБ сохранил ставку 14%\nРешение совета директоров.\n</source>' in prompt
    )
    assert '<source n="2" source="rbc">\nНовость номер 2\n</source>' in prompt
    assert "номер `n` от 1 до 2." in prompt
    assert "Прошлый ответ" not in prompt
    assert prompt.rstrip().endswith("Ответ — только JSON, элементов: 2.")


async def test_long_item_is_limited(fake):
    gateway = fake()
    await news_filter([item(1, summary="слово " * 330)])
    body = gateway.prompts[0].split('<source n="1" source="rbc">\n', 1)[1].split("\n</source>", 1)[0]
    assert len(body) <= extract.FILTER_ITEM_LIMIT
    assert body.endswith("[обрезано]")


async def test_missing_answer_is_asked_again_only_for_missing(fake):
    def answer(ns):
        if len(ns) == 5:  # первая пачка: пропускаем 2 и 4
            return [{"n": n, "business": False, "reason": "нет"} for n in ns if n not in (2, 4)]
        return [{"n": n, "business": False, "reason": f"повтор {n}"} for n in ns]

    gateway = fake(answer=answer)
    items = [item(n) for n in range(1, 6)]
    results = await news_filter(items)
    assert [len(numbers(p)) for p in gateway.prompts] == [5, 2]
    retry = gateway.prompts[1]
    assert "Прошлый ответ был неполным" in retry
    assert "Прошлый ответ" not in gateway.prompts[0]
    assert "Новость номер 2\n" in retry and "Новость номер 4\n" in retry
    assert "Новость номер 1\n" not in retry
    assert [r.reason for r in results] == ["нет", "повтор 1", "нет", "повтор 2", "нет"]


async def test_still_missing_after_retry_passes_through(fake):
    gateway = fake(answer=lambda ns: [{"n": 1, "business": False, "reason": "нет"}])
    results = await news_filter([item(1), item(2), item(3)])
    assert len(gateway.prompts) == 2
    assert [(r.business, r.reason) for r in results] == [
        (False, "нет"),
        (False, "нет"),  # перегенерация: номер 1 новой пачки — это публикация 2
        (True, NO_ANSWER_REASON),
    ]


async def test_extra_and_repeated_numbers_are_ignored(fake):
    def answer(ns):
        return [
            {"n": 1, "business": False, "reason": "первый"},
            {"n": 1, "business": True, "reason": "повтор"},
            {"n": 2, "business": True, "reason": "да"},
            {"n": 7, "business": True, "reason": "лишний"},
            {"n": 0, "business": True, "reason": "ноль"},
        ]

    gateway = fake(answer=answer)
    results = await news_filter([item(1), item(2)])
    assert len(gateway.prompts) == 1
    assert [(r.business, r.reason) for r in results] == [(False, "первый"), (True, "да")]


@pytest.mark.parametrize(
    "error", [LLMNotConfigured("нет ключа"), BudgetExceeded("лимит"), AllModelsFailed("все упали")]
)
async def test_llm_unavailable_passes_everything(fake, error):
    fake(error=error)
    results = await news_filter([item(n) for n in range(1, 4)])
    assert [(r.business, r.reason) for r in results] == [(True, NO_LLM_REASON)] * 3


async def test_one_failed_batch_does_not_spoil_others(fake, monkeypatch):
    gateway = fake()
    real_json = gateway.json

    async def flaky(task, prompt, variables, schema, **kwargs):
        if "Новость номер 11\n" in variables["sources"]:
            raise AllModelsFailed("вторая пачка")
        return await real_json(task, prompt, variables, schema, **kwargs)

    monkeypatch.setattr(gateway, "json", flaky)
    results = await news_filter([item(n) for n in range(1, 13)])
    assert [r.reason for r in results[:10]] == [f"r{n}" for n in range(1, 11)]
    assert [r.reason for r in results[10:]] == [NO_LLM_REASON] * 2


async def test_reason_is_trimmed_and_never_empty(fake):
    fake(
        answer=lambda ns: [
            {"n": 1, "business": True, "reason": "я" * 500},
            {"n": 2, "business": True, "reason": " "},
        ]
    )
    long, empty = await news_filter([item(1), item(2)])
    assert len(long.reason) == 200
    assert empty.reason == EMPTY_REASON


async def test_markup_injections_are_quarantined_without_the_model(fake):
    gateway = fake()
    rows = [r for r in INJECTIONS if {"source_tag", "markup_tag"} & set(r["expect_flags"])]
    assert len(rows) >= 3
    items = [item(n, row["title"], row["text"][:1900]) for n, row in enumerate(rows, 1)] + [item(99)]
    results = await news_filter(items)
    assert all(not r.business and r.reason.startswith("подозрение на инъекцию: ") for r in results[:-1])
    assert numbers(gateway.prompts[0]) == [1]
    assert "Новость номер 99" in gateway.prompts[0]


async def test_command_phrases_go_to_the_model_marked(fake):
    rows = [r for r in INJECTIONS if r["expect_flags"] == ["command"]]
    assert rows
    real = "Минцифры утвердило инструкции для ИИ-сервисов банков"
    gateway = fake()
    results = await news_filter(
        [item(1, real)] + [item(n, r["title"], r["text"]) for n, r in enumerate(rows, 2)]
    )
    assert [r.reason for r in results] == ["r1"] * (len(rows) + 1)  # каждая — отдельным вызовом
    assert all("[!команда] " in call["vars"]["sources"] for call in gateway.calls)


async def test_quarantined_items_keep_positions_across_batches(fake):
    bad = next(r for r in INJECTIONS if "source_tag" in r["expect_flags"])
    items = [item(n) for n in range(1, 13)]
    items.insert(3, item(50, bad["title"], bad["text"]))
    items.insert(11, item(51, bad["title"], bad["text"]))
    gateway = fake(answer=lambda ns: [{"n": n, "business": True, "reason": f"ок {n}"} for n in ns])
    results = await news_filter(items)
    assert [r.raw_item_id for r in results] == [i.id for i in items]
    assert [r.business for r in results].count(False) == 2
    assert not results[3].business and not results[11].business
    assert sorted(len(numbers(p)) for p in gateway.prompts) == [2, 10]


async def test_same_item_twice_gets_two_answers(fake):
    fake()
    first, second = await news_filter([item(1), item(1)])
    assert first.raw_item_id == second.raw_item_id
    assert (first.reason, second.reason) == ("r1", "r2")


async def test_failed_retry_keeps_answers_already_received(fake, monkeypatch):
    gateway = fake(answer=lambda ns: [{"n": n, "business": False, "reason": "нет"} for n in ns if n != 3])
    real_json = gateway.json

    async def retry_fails(task, prompt, variables, schema, **kwargs):
        if variables["retry"]:
            raise BudgetExceeded("кончился бюджет")
        return await real_json(task, prompt, variables, schema, **kwargs)

    monkeypatch.setattr(gateway, "json", retry_fails)
    results = await news_filter([item(n) for n in range(1, 5)])
    assert [(r.business, r.reason) for r in results] == [
        (False, "нет"),
        (False, "нет"),
        (True, NO_LLM_REASON),
        (False, "нет"),
    ]


async def test_no_more_than_three_batches_at_once(fake, monkeypatch):
    gateway = fake()
    real_json = gateway.json
    running = peak = 0

    async def counting(*args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.01)
            return await real_json(*args, **kwargs)
        finally:
            running -= 1

    monkeypatch.setattr(gateway, "json", counting)
    results = await news_filter([item(n) for n in range(1, 81)])
    assert len(results) == 80
    assert peak == extract.FILTER_CONCURRENCY == 2


async def test_base64_alone_is_not_quarantined(fake):
    row = next(r for r in INJECTIONS if r["expect_flags"] == ["base64"])
    gateway = fake()
    (result,) = await news_filter([item(1, row["title"], row["text"])])
    assert result.reason == "r1"
    assert "[base64 опущен" in gateway.prompts[0]


def test_batch_schema_is_strict_compatible():
    schema = NewsFilterBatch.model_json_schema()
    verdict = schema["$defs"]["_Verdict"]
    assert set(verdict["required"]) == {"n", "business", "reason"}


# --- сквозной путь через настоящий шлюз на фальшивом провайдере ---------------------------------------

BASE = "https://ai.api.cloud.yandex.net/v1"


def completion(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120},
        },
    )


async def test_real_gateway_round_trip(monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x:x@127.0.0.1:1/x",
        demo_password="x",
        llm_api_key=SecretStr("test-key"),
        llm_folder_id=SecretStr("folder"),
        llm_base_url="",
        llm_daily_budget_rub=480.0,
    )
    gateway = Gateway(
        load_config(REPO_ROOT / "config" / "models.yaml"), settings=settings, store=MemoryStore(), caps={}
    )
    monkeypatch.setattr(extract, "gw", gateway)
    answer = {
        "items": [
            {"n": 1, "business": True, "reason": "решение ЦБ по ставке"},
            {"n": 2, "business": False, "reason": "спорт"},
        ]
    }
    with respx.mock(base_url=BASE, assert_all_called=True) as api:
        route = api.post("/chat/completions").mock(return_value=completion(answer))
        results = await news_filter([item(1, "ЦБ сохранил ставку 14%"), item(2, "Зенит выиграл матч")])
    assert [(r.business, r.reason) for r in results] == [(True, "решение ЦБ по ставке"), (False, "спорт")]
    body = json.loads(route.calls[0].request.content)
    content = body["messages"][0]["content"]
    assert numbers(content) == [1, 2]
    assert "ЦБ сохранил ставку 14%" in content


def test_dev_cases_are_well_formed():
    rows = [
        json.loads(line)
        for line in (REPO_ROOT / "tests" / "data" / "news_filter_cases.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 20
    assert sum(r["business"] for r in rows) == 12
    for row in rows:
        RawItemIn(
            id=1,
            source_id="rbc",
            title=row["title"],
            summary=row["summary"],
            url="https://example.test/1",
            published_at="2026-10-20T10:00:00+03:00",
        )
        assert row["why"]


async def test_command_items_are_asked_alone(fake):
    rows = [r for r in INJECTIONS if r["expect_flags"] == ["command"]]
    gateway = fake()
    items = [item(n) for n in range(1, 4)] + [
        item(10 + n, r["title"], r["text"]) for n, r in enumerate(rows[:2])
    ]
    results = await news_filter(items)
    assert sorted(len(numbers(p)) for p in gateway.prompts) == [1, 1, 3]
    shared = [c["vars"]["sources"] for c in gateway.calls if c["vars"]["count"] == 3]
    assert len(shared) == 1 and "[!команда] " not in shared[0]
    assert [r.raw_item_id for r in results] == [i.id for i in items]


async def test_isolated_calls_are_bounded(fake):
    row = next(r for r in INJECTIONS if r["expect_flags"] == ["command"])
    gateway = fake()
    items = [item(n, row["title"], row["text"]) for n in range(1, 26)] + [item(99)]
    results = await news_filter(items)
    sizes = sorted(len(numbers(p)) for p in gateway.prompts)
    assert sizes == [1] * (extract.FILTER_ISOLATED_MAX + 1)  # десять помеченных и пачка с чистой новостью
    assert len(results) == 26
    over = results[extract.FILTER_ISOLATED_MAX : 25]
    assert [(r.business, r.reason) for r in over] == [(True, extract.OVER_ISOLATED_REASON)] * 15
    clean = [c["vars"]["sources"] for c in gateway.calls if "Новость номер 99" in c["vars"]["sources"]]
    assert len(clean) == 1 and "[!команда] " not in clean[0]


async def test_failed_retry_reason_says_no_llm(fake, monkeypatch):
    gateway = fake(answer=lambda ns: [{"n": n, "business": False, "reason": "нет"} for n in ns if n != 2])
    real_json = gateway.json

    async def retry_fails(task, prompt, variables, schema, **kwargs):
        if variables["retry"]:
            raise AllModelsFailed("упали")
        return await real_json(task, prompt, variables, schema, **kwargs)

    monkeypatch.setattr(gateway, "json", retry_fails)
    results = await news_filter([item(1), item(2)])
    assert [r.reason for r in results] == ["нет", NO_LLM_REASON]
