import json
import re
import unicodedata

import pytest

from app.settings import REPO_ROOT
from app.text.sanitize import Flag, neutralize, render_sources, wrap_source

ROWS = [
    json.loads(line)
    for line in (REPO_ROOT / "tests" / "data" / "injections.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{64,}")


def test_dataset_has_ten_distinct_injections():
    assert len(ROWS) == 10
    assert len({r["id"] for r in ROWS}) == 10
    for row in ROWS:
        assert set(row) == {
            "id",
            "title",
            "text",
            "technique",
            "expect_flags",
            "must_keep",
            "must_not_contain",
        }
        assert {Flag(f) for f in row["expect_flags"]} <= set(Flag)


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_injection_stays_inside_its_source(row):
    wrapped = wrap_source(1, f"{row['title']}\n{row['text']}", kind="news")
    lines = wrapped.text.split("\n")
    inner = "\n".join(lines[1:-1])

    assert lines[0] == '<source n="1" kind="news">'
    assert lines[-1] == "</source>"
    assert not re.search(r"<\s*/?\s*source", inner, re.I)
    assert "<|" not in inner and "|>" not in inner
    assert not any(unicodedata.category(ch) in {"Cf", "Co", "Cn", "Cs"} for ch in inner)
    assert not B64_RUN.search(inner)

    assert {Flag(f) for f in row["expect_flags"]} <= wrapped.flags
    for phrase in row["must_keep"]:
        assert phrase in inner, phrase
    for bad in row["must_not_contain"]:
        assert bad not in inner, bad


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_preparing_twice_changes_nothing(row):
    once = neutralize(row["text"]).text
    assert neutralize(once).text == once


def test_whole_batch_keeps_ten_separate_sources():
    rendered = render_sources([f"{r['title']}\n{r['text']}" for r in ROWS], limit=600)
    assert re.findall(r'^<source n="(\d+)">$', rendered.text, re.M) == [str(n) for n in range(1, 11)]
    assert rendered.text.count("</source>") == 10
    assert {Flag.COMMAND, Flag.SOURCE_TAG, Flag.MARKUP_TAG, Flag.BASE64} <= rendered.flags
