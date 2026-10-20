import json
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from app.agent import run
from app.api import mock
from app.api.errors import ErrorBody
from app.contracts import AgentStep, ChatAnswer, InsightResult
from app.fixtures import load_fixture

STEP_COUNT = len(load_fixture("trace.json", TypeAdapter(list[AgentStep])))


def parse(body: str) -> list[dict]:
    frames = []
    for block in body.split("\n\n"):
        if not block:
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        frames.append({"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])})
    return frames


def test_insight_stream_sequence(client):
    response = client.get("/api/insights/1/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    frames = parse(response.text)
    assert [f["event"] for f in frames] == ["step"] * STEP_COUNT + ["insight", "done"]
    assert [f["id"] for f in frames] == list(range(1, STEP_COUNT + 3))
    AgentStep.model_validate(frames[0]["data"])
    InsightResult.model_validate(frames[STEP_COUNT]["data"])


def test_reconnect_gets_only_frames_after_last_event_id(client):
    full = parse(client.get("/api/insights/1/stream").text)
    resumed = parse(client.get("/api/insights/1/stream", headers={"Last-Event-ID": "3"}).text)
    assert resumed[0]["id"] == 4
    assert resumed == full[3:]


def test_reconnect_after_done_gets_empty_stream(client):
    done_id = len(parse(client.get("/api/insights/1/stream").text))
    response = client.get("/api/insights/1/stream", headers={"Last-Event-ID": str(done_id)})
    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.parametrize("header", ["abc", "-1", ""])
def test_garbage_last_event_id_means_full_stream(client, header):
    frames = parse(client.get("/api/insights/1/stream", headers={"Last-Event-ID": header}).text)
    assert frames[0]["id"] == 1


def test_chat_stream_sequence(client):
    response = client.post("/api/chat", json={"event_id": 1, "question": "Что именно говорит банк?"})
    assert response.status_code == 200
    frames = parse(response.text)
    assert [f["event"] for f in frames] == ["step", "step", "step", "answer", "done"]
    assert [f["id"] for f in frames] == [1, 2, 3, 4, 5]
    ChatAnswer.model_validate(frames[3]["data"])


def test_chat_without_event_and_with_chat_id(client):
    body = {"event_id": None, "chat_id": "6f1c2f0e-8f5a-4c52-9a53-0c3b9d7e1a11", "question": "Что в ленте?"}
    frames = parse(client.post("/api/chat", json=body).text)
    assert frames[-2]["event"] == "answer"


def test_chat_question_length_is_validated(client):
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
    assert client.post("/api/chat", json={"question": "а" * 501}).status_code == 422


def test_generator_failure_becomes_error_frame_then_done(client, monkeypatch):
    async def broken(after):
        yield load_fixture("trace.json", TypeAdapter(list[AgentStep]))[0]
        raise RuntimeError("внутренняя причина")

    monkeypatch.setattr(mock, "_insight_stream", broken)
    response = client.get("/api/insights/1/stream")
    assert response.status_code == 200
    frames = parse(response.text)
    assert [f["event"] for f in frames] == ["step", "error", "done"]
    error = ErrorBody.model_validate(frames[1]["data"]).error
    assert error.code == "internal_error"
    assert "внутренняя причина" not in response.text


def test_domain_error_in_generator_keeps_its_code(client, monkeypatch):
    async def missing(after):
        raise LookupError("нет данных")
        yield

    monkeypatch.setattr(mock, "_insight_stream", missing)
    frames = parse(client.get("/api/insights/1/stream").text)
    assert [f["event"] for f in frames] == ["error", "done"]
    assert frames[0]["data"]["error"]["code"] == "not_found"


@pytest.fixture
def sleeps(monkeypatch):
    """Подсчёт пауз между шагами вместо реального ожидания."""
    pauses: list[float] = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(run, "STEP_DELAY_S", (0.5, 0.5))
    monkeypatch.setattr(mock, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return pauses


def test_full_stream_pauses_before_every_step(client, sleeps):
    client.get("/api/insights/1/stream")
    assert sleeps == [0.5] * STEP_COUNT


def test_reconnect_does_not_pause_for_frames_already_delivered(client, sleeps):
    resumed = parse(client.get("/api/insights/1/stream", headers={"Last-Event-ID": "10"}).text)
    assert [f["id"] for f in resumed] == [11, 12, 13, 14]
    assert sleeps == [0.5] * (STEP_COUNT - 10)


def test_chat_reconnect_does_not_pause_for_frames_already_delivered(client, sleeps):
    body = {"event_id": 1, "question": "Что именно говорит банк?"}
    resumed = parse(client.post("/api/chat", json=body, headers={"Last-Event-ID": "2"}).text)
    assert [f["event"] for f in resumed] == ["step", "answer", "done"]
    assert sleeps == [0.5]
